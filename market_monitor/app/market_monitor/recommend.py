"""Aanbeveling per markt: een vast handelsplan met een live status, dat wijzigt wanneer het echt nodig is.

Elke paar minuten (zie app.py) wordt de aanbeveling tegen de actuele koers gehouden:

  status   in de instapzone / boven of onder de instapzone (niet najagen) / doel 1 gehaald / doel 2 gehaald /
           stop-loss geraakt
  wijzigen  * stop-loss geraakt of doel 2 gehaald  -> plan sluiten, nieuwe beste kandidaat
            * doel 1 gehaald                       -> stop naar de instapprijs (winst veiligstellen)
            * de richting van de techniek + koopscore slaat om (long -> short/afwachten) -> nieuw plan
            * een andere kandidaat is duidelijk sterker (30% beter, na minimaal een uur)  -> wisselen
            * na 24 uur buiten de instapzone       -> instap, stop en doelen bijwerken naar de huidige koers
Instap, stop en doelen staan dus vast zolang het plan geldt; zo zie je of het plan uitkomt. Na een geraakte
stop wordt dezelfde kant van dat aandeel/die munt 24 uur niet opnieuw aanbevolen.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

from .db import Database, iso, parse_iso, utcnow
from .technical import build_plan

log = logging.getLogger(__name__)

MIN_HISTORY = 60
SWITCH_FACTOR = 1.3
MIN_HOLD_MINUTES = 60
REISSUE_HOURS = 24
COOLDOWN_HOURS = 24
FIXED = ("ticker", "name", "direction", "label", "action", "conviction", "entry_low", "entry_high", "entry",
         "stop", "target1", "target2", "rr", "prob")


def plan_status(rec: dict, price: float | None) -> dict:
    """Waar staat de koers ten opzichte van het (vaste) plan?"""
    d = rec.get("direction")
    if d not in ("long", "short") or price is None:
        return {"code": "neutral", "text": "Geen positie: afwachten tot er een duidelijke richting is"}
    lo, hi, stop, t1, t2 = rec["entry_low"], rec["entry_high"], rec["stop"], rec["target1"], rec["target2"]
    if d == "long":
        if price <= stop:
            return {"code": "stop", "text": "Stop-loss geraakt: het plan vervalt"}
        if price >= t2:
            return {"code": "target2", "text": "Doel 2 gehaald: winst nemen"}
        if price >= t1:
            return {"code": "target1", "text": "Doel 1 gehaald: (deel van de) winst nemen, stop naar de instapprijs"}
        if lo <= price <= hi:
            return {"code": "in_zone", "text": "Koers in de instapzone: instappen kan nu"}
        if price > hi:
            return {"code": "above_zone", "text": "Koers boven de instapzone: niet najagen, wacht op een terugval"}
        return {"code": "below_zone", "text": "Koers onder de instapzone: let goed op de stop-loss"}
    if price >= stop:
        return {"code": "stop", "text": "Stop-loss geraakt: het plan vervalt"}
    if price <= t2:
        return {"code": "target2", "text": "Doel 2 gehaald: winst nemen"}
    if price <= t1:
        return {"code": "target1", "text": "Doel 1 gehaald: (deel van de) winst nemen, stop naar de instapprijs"}
    if lo <= price <= hi:
        return {"code": "in_zone", "text": "Koers in de instapzone: instappen kan nu"}
    if price < lo:
        return {"code": "below_zone", "text": "Koers onder de instapzone: niet najagen, wacht op een opleving"}
    return {"code": "above_zone", "text": "Koers boven de instapzone: let goed op de stop-loss"}


class Recommender:
    def __init__(self, cfg, db: Database):
        self.cfg = cfg
        self.db = db

    # ------------------------------------------------------------------ plan voor één ticker
    def plan_for(self, market: str, ticker: str, score_row: dict | None = None) -> dict | None:
        rows = self.db.query(
            "SELECT date, close FROM prices WHERE market=? AND ticker=? ORDER BY date DESC LIMIT 200", (market, ticker)
        )[::-1]
        if len(rows) < MIN_HISTORY:
            return None
        sc = score_row or self.db.query_one("SELECT * FROM scores_latest WHERE market=? AND ticker=?", (market, ticker))
        meta = self.db.query_one("SELECT * FROM price_meta WHERE market=? AND ticker=?", (market, ticker)) or {}
        closes = [r["close"] for r in rows]
        dates = [r["date"] for r in rows]
        # Laatste live koers meenemen als die nieuwer is dan de laatste slotkoers
        if meta.get("last_price") and (meta.get("fetched_at") or "")[:10] >= dates[-1]:
            closes[-1] = meta["last_price"]
        score = (sc or {}).get("score", 50.0)
        prob = (sc or {}).get("prob_up") or 0.5
        result = build_plan(dates, closes, score, prob, market)
        reasons = json.loads(sc["reasons"]) if sc and isinstance(sc.get("reasons"), str) else (sc or {}).get("reasons", [])
        result.update({
            "market": market,
            "ticker": ticker,
            "name": meta.get("name"),
            "currency": meta.get("currency") or "USD",
            "chg_24h": meta.get("chg_24h"),
            "price_at": meta.get("fetched_at"),
            "score": score,
            "prob_up": prob,
            "prob_source": (sc or {}).get("prob_source", "model"),
            "timing": (sc or {}).get("timing"),
            "reasons": reasons or [],
        })
        return result

    @staticmethod
    def rank(p: dict | None) -> float:
        if not p:
            return 0.0
        plan = p["plan"]
        if plan["direction"] == "neutral":
            return 0.0
        rr = min(plan.get("rr") or 0, 3) / 3
        return plan["conviction"] * (0.7 + 0.3 * rr)

    def candidates(self, market: str) -> list[dict]:
        rows = self.db.query("SELECT * FROM scores_latest WHERE market=?", (market,))
        rows.sort(key=lambda r: -abs(r["score"] - 50))
        plans = []
        for r in rows[:12]:
            try:
                p = self.plan_for(market, r["ticker"], r)
            except Exception as exc:  # noqa: BLE001 - één ticker met rare data mag de rest niet tegenhouden
                log.warning("Technische analyse %s mislukt: %s", r["ticker"], exc)
                continue
            if p:
                plans.append(p)
        return plans

    def _cooldown(self, market: str) -> dict:
        cd = self.db.get_state(f"reco_cooldown_{market}") or {}
        now = iso(utcnow())
        return {t: v for t, v in cd.items() if v.get("until", "") > now}

    def best(self, market: str, plans: list[dict]) -> dict | None:
        cd = self._cooldown(market)
        allowed = [p for p in plans if not (p["ticker"] in cd and cd[p["ticker"]]["direction"] == p["plan"]["direction"])]
        directional = [p for p in allowed if p["plan"]["direction"] != "neutral"]
        if directional:
            return max(directional, key=self.rank)
        return max(allowed or plans, key=lambda p: p["score"]) if (allowed or plans) else None

    # ------------------------------------------------------------------ evaluatie (elke paar minuten)
    def update(self, market: str) -> dict | None:
        """Houdt de aanbeveling tegen de actuele situatie. Geeft {"state", "changed", "event", "reason", ...}."""
        plans = self.candidates(market)
        if not plans:
            return None
        by_ticker = {p["ticker"]: p for p in plans}
        cur = self.summary(market)
        pick = self.best(market, plans)
        if not cur or not cur.get("ticker") or "entry_low" not in cur:
            return self._issue(market, pick, "Eerste aanbeveling", None, event=None)

        live = by_ticker.get(cur["ticker"])
        if live is None:
            try:
                live = self.plan_for(market, cur["ticker"])
            except Exception:  # noqa: BLE001
                live = None
        price = live["plan"]["price"] if live else cur.get("live_price", cur.get("price"))
        status = plan_status(cur, price)
        now = utcnow()
        age = now - (parse_iso(cur.get("since")) or now)

        if cur["direction"] in ("long", "short"):
            if status["code"] in ("stop", "target2"):
                outcome = "stop geraakt" if status["code"] == "stop" else "doel 2 gehaald"
                self._close(market, cur, outcome, price)
                if status["code"] == "stop":
                    self._set_cooldown(market, cur)
                    pick = self.best(market, plans)
                verb = "Stop-loss" if status["code"] == "stop" else "Doel 2"
                return self._issue(market, pick, f"{verb} van {cur['label']} {cur['ticker']} geraakt",
                                   cur, event="stop" if status["code"] == "stop" else "target2", close_price=price)
            if status["code"] == "target1" and not cur.get("target1_hit"):
                cur["target1_hit"] = True
                cur["stop"] = cur.get("entry") or (cur["entry_low"] + cur["entry_high"]) / 2
                cur.update(live_price=price, status=plan_status(cur, price), updated_at=iso(now),
                           change_reason="Doel 1 gehaald: stop verplaatst naar de instapprijs",
                           changed_at=iso(now))
                self.db.set_state(f"recommendation_{market}", cur)
                return {"state": cur, "changed": False, "event": "target1", "reason": cur["change_reason"],
                        "previous": None}

        # Afwachten, maar er is nu een duidelijke kans
        if cur["direction"] == "neutral" and pick and pick["plan"]["direction"] != "neutral":
            return self._issue(market, pick, f"Nieuwe kans: {pick['plan']['label']} {pick['ticker']}", cur,
                               event="new", close_price=price)

        # De richting van het huidige plan slaat om (met marge, zodat hij niet heen en weer springt)
        fresh_dir = live["plan"]["direction"] if live else cur["direction"]
        flipped = live is not None and cur["direction"] in ("long", "short") and (
            (fresh_dir in ("long", "short") and fresh_dir != cur["direction"])
            or (fresh_dir == "neutral" and abs(live["plan"]["combined"]) < 0.1))
        if flipped and not cur.get("target1_hit"):
            new_dir = live["plan"]["label"]
            self._close(market, cur, "richting gewijzigd", price)
            target = pick if self.rank(pick) > self.rank(live) else live
            return self._issue(market, target, f"Signaal voor {cur['ticker']} gewijzigd van {cur['label']} naar "
                               f"{new_dir}", cur, event="direction", close_price=price)

        # Een andere kandidaat is duidelijk sterker
        if (pick and pick["ticker"] != cur["ticker"] and age >= timedelta(minutes=MIN_HOLD_MINUTES)
                and status["code"] != "target1"
                and self.rank(pick) >= SWITCH_FACTOR * max(self.rank(live), 1.0)):
            self._close(market, cur, "vervangen door sterkere kandidaat", price)
            return self._issue(market, pick, f"{pick['ticker']} heeft nu een duidelijk sterker signaal dan "
                               f"{cur['ticker']}", cur, event="switch", close_price=price)

        # Lang buiten de instapzone: plan bijwerken naar de huidige koers
        if (live and age >= timedelta(hours=REISSUE_HOURS) and status["code"] in ("above_zone", "below_zone")
                and live["plan"]["direction"] == cur["direction"]):
            self._close(market, cur, "plan bijgewerkt", price)
            return self._issue(market, live, "Instapzone, stop en doelen bijgewerkt naar de huidige koers", cur,
                               event="refresh", close_price=price)

        # Geen wijziging: live gegevens bijwerken
        cur.update(live_price=price, status=status, updated_at=iso(now))
        if live:
            cur["live_conviction"] = live["plan"]["conviction"]
            cur["signals"] = live["plan"]["signals"][:5]
        self.db.set_state(f"recommendation_{market}", cur)
        return {"state": cur, "changed": False, "event": None, "reason": None, "previous": None}

    # ------------------------------------------------------------------ uitgeven / sluiten
    def _issue(self, market: str, pick: dict | None, reason: str, prev: dict | None, event: str | None,
               close_price: float | None = None) -> dict | None:
        if pick is None:
            return None
        plan = pick["plan"]
        now = iso(utcnow())
        state = {k: plan.get(k) for k in FIXED if k != "ticker" and k != "name"}
        state.update({
            "ticker": pick["ticker"], "name": pick["name"], "price": plan["price"], "live_price": plan["price"],
            "signals": plan["signals"][:5], "since": now, "updated_at": now, "issued_price": plan["price"],
            "change_reason": reason, "changed_at": now, "target1_hit": False,
            "previous": ({"ticker": prev["ticker"], "label": prev["label"], "direction": prev["direction"],
                          "close_price": close_price} if prev else None),
        })
        state["status"] = plan_status(state, plan["price"])
        self.db.set_state(f"recommendation_{market}", state)
        self.db.execute(
            "INSERT INTO recommendations(market, ticker, direction, created_at, price, entry, stop, target1,"
            " target2, plan) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (market, state["ticker"], state["direction"], now, plan["price"], plan["entry"], plan["stop"],
             plan["target1"], plan["target2"], json.dumps(state, ensure_ascii=False)),
        )
        log.info("Aanbeveling %s: %s %s (%s)", market, state["label"], state["ticker"], reason)
        return {"state": state, "changed": True, "event": event, "reason": reason, "previous": prev, "pick": pick}

    def _close(self, market: str, rec: dict, outcome: str, price: float | None) -> None:
        row = self.db.query_one(
            "SELECT id FROM recommendations WHERE market=? AND ticker=? AND direction=? AND closed_at IS NULL"
            " ORDER BY id DESC LIMIT 1", (market, rec["ticker"], rec["direction"]),
        )
        if row:
            self.db.execute("UPDATE recommendations SET closed_at=?, outcome=?, close_price=? WHERE id=?",
                            (iso(utcnow()), outcome, price, row["id"]))

    def _set_cooldown(self, market: str, rec: dict) -> None:
        cd = self._cooldown(market)
        cd[rec["ticker"]] = {"direction": rec["direction"],
                             "until": iso(utcnow() + timedelta(hours=COOLDOWN_HOURS))}
        self.db.set_state(f"reco_cooldown_{market}", cd)

    # ------------------------------------------------------------------ voor het dashboard
    def summary(self, market: str) -> dict | None:
        return self.db.get_state(f"recommendation_{market}")

    def current(self, market: str) -> dict | None:
        """Aanbeveling met verse grafiekdata, maar met de vaste niveaus van het geldende plan."""
        s = self.summary(market)
        if not s or "entry_low" not in s:
            return None
        p = self.plan_for(market, s["ticker"])
        if not p:
            return None
        fresh = p["plan"]
        plan = dict(fresh)
        for k in FIXED:
            if k in s and k not in ("ticker", "name"):
                plan[k] = s[k]
        price = fresh["price"]
        if plan["direction"] in ("long", "short") and plan.get("entry") and plan.get("stop"):
            risk = abs(plan["entry"] - plan["stop"])
            plan["risk_pct"] = round(risk / plan["entry"] * 100, 1) if plan["entry"] else None
            plan["reward_pct"] = round(abs(plan["target1"] - plan["entry"]) / plan["entry"] * 100, 1)
            half = fresh["horizon_days"] // 2
            p["chart"]["scenario"] = [[0, price], [half, plan["target1"]], [fresh["horizon_days"], plan["target2"]]]
        else:
            p["chart"]["scenario"] = []
        p["plan"] = plan
        p["fresh"] = {k: fresh[k] for k in ("direction", "label", "conviction", "combined")}
        p.update({
            "status": plan_status(s, price),
            "since": s.get("since"),
            "updated_at": s.get("updated_at"),
            "issued_price": s.get("issued_price"),
            "change_reason": s.get("change_reason"),
            "changed_at": s.get("changed_at"),
            "previous": s.get("previous"),
            "target1_hit": s.get("target1_hit", False),
        })
        return p

    # ------------------------------------------------------------------ geschiedenis
    def history(self, market: str, limit: int = 6) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM recommendations WHERE market=? ORDER BY id DESC LIMIT ?", (market, limit)
        )
        out = []
        for r in rows:
            ref = r["entry"] or r["price"]
            if r.get("closed_at"):
                end = r["close_price"] or ref
                ret = None
                if ref and end and r["direction"] == "long":
                    ret = (end / ref - 1) * 100
                elif ref and end and r["direction"] == "short":
                    ret = (ref / end - 1) * 100
                out.append({"ticker": r["ticker"], "direction": r["direction"], "created_at": r["created_at"],
                            "closed_at": r["closed_at"], "price": r["price"], "entry": r["entry"], "now": end,
                            "return_pct": round(ret, 2) if ret is not None else None,
                            "status": r["outcome"] or "gesloten"})
                continue
            closes = self.db.query(
                "SELECT date, close FROM prices WHERE market=? AND ticker=? AND date > ? ORDER BY date",
                (market, r["ticker"], r["created_at"][:10]),
            )
            meta = self.db.query_one("SELECT last_price FROM price_meta WHERE market=? AND ticker=?",
                                     (market, r["ticker"])) or {}
            now_price = meta.get("last_price") or (closes[-1]["close"] if closes else r["price"])
            status, ret = "loopt", None
            if r["direction"] == "long" and ref:
                ret = (now_price / ref - 1) * 100
                for c in closes:
                    if r["stop"] and c["close"] <= r["stop"]:
                        status = "stop geraakt"
                        break
                    if r["target1"] and c["close"] >= r["target1"]:
                        status = "doel 1 gehaald"
                        break
            elif r["direction"] == "short" and ref:
                ret = (ref / now_price - 1) * 100
                for c in closes:
                    if r["stop"] and c["close"] >= r["stop"]:
                        status = "stop geraakt"
                        break
                    if r["target1"] and c["close"] <= r["target1"]:
                        status = "doel 1 gehaald"
                        break
            else:
                status = "geen positie"
            out.append({"ticker": r["ticker"], "direction": r["direction"], "created_at": r["created_at"],
                        "closed_at": None, "price": r["price"], "entry": r["entry"], "now": now_price,
                        "return_pct": round(ret, 2) if ret is not None else None, "status": status})
        return out
