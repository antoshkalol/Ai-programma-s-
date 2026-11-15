"""Mijn posities: wat je zelf gekocht hebt, met live verkoopadvies en meldingen op je telefoon.

Je geeft bij een aanbeveling (of handmatig) aan dat je iets gekocht hebt: hoeveel euro en tegen welke koers. De
add-on volgt de positie elke paar minuten en zegt wat je moet doen:

  houden          koers tussen de stop en doel 1
  deels verkopen  doel 1 gehaald: ~50% verkopen, stop naar je aankoopprijs (plus kosten) -> de rest kan geen
                  verlies meer worden
  laten lopen     doel 2 gehaald terwijl het momentum nog sterk is: de rest loopt door met een trailing stop
                  (de stop schuift mee met de hoogste koers) - zo pak je zo veel mogelijk van de stijging mee
  verkopen        stop-loss of trailing stop geraakt, of doel 2 gehaald zonder momentum
  let op          de analyse slaat om, negatief nieuws, of het plan duurt te lang

Bij elk 'verkopen'/'deels verkopen'-moment krijg je een melding met hoge prioriteit (zoals een noodmelding).
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

from .config import MARKET_LABELS
from .db import Database, ago, iso, parse_iso, utcnow
from .scoring import fmt_num, fmt_price

log = logging.getLogger(__name__)

TRAIL_ATR = 2.0          # trailing stop op 2x de gemiddelde dagbeweging onder de hoogste koers
BREAKEVEN_PCT = 0.5      # stop naar aankoopprijs + 0,5% (kosten) na doel 1
SLOW_DAYS = 30
SELL_EVENTS = {"stop", "trail", "target2_sell", "target1"}
REPEAT = {"reversal": timedelta(hours=24), "news": timedelta(hours=12)}


def fmt_eur(v: float | None) -> str:
    if v is None:
        return "?"
    return ("-" if v < 0 else "") + "€" + fmt_num(abs(v), 2)


def fpct(v: float | None) -> str:
    """Procent in Nederlandse notatie met teken, bv. +3,4%."""
    if v is None:
        return "?"
    return ("+" if v >= 0 else "-") + fmt_num(abs(v), 1) + "%"


def pct(a: float, b: float) -> float:
    return (a / b - 1) * 100 if b else 0.0


class Holdings:
    def __init__(self, cfg, db: Database, recommender, hass=None, fx=None):
        self.cfg = cfg
        self.db = db
        self.recommender = recommender
        self.hass = hass
        self.fx = fx

    def current_price(self, market: str, ticker: str) -> tuple[float | None, str | None]:
        """Live koers, of anders de laatste slotkoers."""
        meta = self.db.query_one("SELECT last_price, fetched_at FROM price_meta WHERE market=? AND ticker=?",
                                 (market, ticker)) or {}
        if meta.get("last_price"):
            return meta["last_price"], meta.get("fetched_at")
        row = self.db.query_one("SELECT date, close FROM prices WHERE market=? AND ticker=? ORDER BY date DESC LIMIT 1",
                                (market, ticker))
        return (row["close"], row["date"]) if row else (None, None)

    def rate(self) -> float:
        try:
            return self.fx.eurusd() if self.fx else 1.10
        except Exception:  # noqa: BLE001
            return 1.10

    # ------------------------------------------------------------------ vastleggen
    def default_levels(self, market: str, ticker: str, entry: float) -> dict:
        """Stop en doelen: uit de huidige analyse als die long is, anders op basis van de dagbeweging."""
        try:
            fresh = self.recommender.plan_for(market, ticker)
        except Exception:  # noqa: BLE001
            fresh = None
        fp = (fresh or {}).get("plan") or {}
        atr = fp.get("atr") or entry * (0.04 if market == "crypto" else 0.02)
        if fp.get("direction") == "long" and fp.get("stop") and fp["stop"] < entry < fp["target1"]:
            return {"stop": fp["stop"], "target1": fp["target1"], "target2": fp["target2"], "atr": atr}
        return {"stop": entry - 2 * atr, "target1": entry + 3 * atr, "target2": entry + 5 * atr, "atr": atr}

    def add(self, market: str, ticker: str, amount_eur: float, price: float | None = None, bought_at: str | None = None,
            source: str = "handmatig", rec_id: int | None = None, levels: dict | None = None,
            note: str = "") -> dict:
        ticker = ticker.upper().strip().lstrip("$")
        if market not in ("stocks", "crypto") or not ticker:
            raise ValueError("Kies een markt en een aandeel of munt")
        if not amount_eur or amount_eur <= 0:
            raise ValueError("Vul het bedrag in euro in")
        if not price:
            price, _ = self.current_price(market, ticker)
        if not price or price <= 0:
            raise ValueError(f"Geen koers bekend voor {ticker}: vul de aankoopkoers in")
        fx = self.rate()
        qty = amount_eur * fx / price
        lv = dict(levels or {})
        if rec_id and not lv:
            row = self.db.query_one("SELECT plan FROM recommendations WHERE id=?", (rec_id,))
            plan = json.loads(row["plan"]) if row and row.get("plan") else {}
            if plan.get("direction") == "long":
                lv = {k: plan.get(k) for k in ("stop", "target1", "target2")}
        if not lv.get("stop") or not lv.get("target1") or not (lv["stop"] < price < lv["target1"]):
            lv = {**self.default_levels(market, ticker, price), **{k: v for k, v in lv.items() if k == "atr" and v}}
        if not lv.get("atr"):
            lv["atr"] = self.default_levels(market, ticker, price)["atr"]
        if not lv.get("target2") or lv["target2"] <= lv["target1"]:
            lv["target2"] = lv["target1"] + 2 * lv["atr"]
        now = iso(utcnow())
        self.db.execute(
            "INSERT INTO user_holdings(market, ticker, qty, entry_price, amount_eur, fx, bought_at, source, rec_id,"
            " stop, target1, target2, atr, high_water, note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (market, ticker, qty, price, amount_eur, fx, bought_at or now, source, rec_id, lv["stop"], lv["target1"],
             lv["target2"], lv["atr"], price, note[:300], now),
        )
        hid = self.db.query_one("SELECT MAX(id) id FROM user_holdings")["id"]
        log.info("Positie toegevoegd: %s %s voor %s", market, ticker, fmt_eur(amount_eur))
        return self.get(hid)

    def get(self, hid: int) -> dict | None:
        h = self.db.query_one("SELECT * FROM user_holdings WHERE id=?", (hid,))
        if h:
            h["advice"] = json.loads(h["advice"]) if h.get("advice") else None
            h["notified"] = json.loads(h["notified"] or "{}")
        return h

    def update(self, hid: int, fields: dict) -> dict | None:
        allowed = {"stop": float, "target1": float, "target2": float, "note": str, "amount_eur": float,
                   "entry_price": float}
        sets, args = [], []
        for k, typ in allowed.items():
            if k in fields and fields[k] not in (None, ""):
                try:
                    val = typ(str(fields[k]).replace(",", ".")) if typ is float else str(fields[k])[:300]
                except ValueError as exc:
                    raise ValueError(f"Ongeldige waarde voor {k}") from exc
                sets.append(f"{k}=?")
                args.append(val)
        h = self.get(hid)
        if not h:
            return None
        if "amount_eur" in fields or "entry_price" in fields:
            amount = float(str(fields.get("amount_eur") or h["amount_eur"]).replace(",", "."))
            entry = float(str(fields.get("entry_price") or h["entry_price"]).replace(",", "."))
            sets.append("qty=?")
            args.append(amount * (h["fx"] or self.rate()) / entry)
        if sets:
            self.db.execute(f"UPDATE user_holdings SET {', '.join(sets)} WHERE id=?", (*args, hid))
        return self.get(hid)

    def close(self, hid: int, price: float | None = None) -> dict | None:
        h = self.get(hid)
        if not h:
            return None
        if not price:
            price = self.current_price(h["market"], h["ticker"])[0] or h["entry_price"]
        self.db.execute("UPDATE user_holdings SET status='closed', closed_at=?, exit_price=? WHERE id=?",
                        (iso(utcnow()), price, hid))
        return self.get(hid)

    def delete(self, hid: int) -> bool:
        return self.db.execute("DELETE FROM user_holdings WHERE id=?", (hid,)) > 0

    # ------------------------------------------------------------------ advies
    def evaluate(self, h: dict, fx: float | None = None) -> dict:
        fx = fx or self.rate()
        price, price_at = self.current_price(h["market"], h["ticker"])
        if h["status"] == "closed":
            price = h["exit_price"]
        if not price:
            return {"code": "unknown", "title": "Geen actuele koers", "text": "Er komt nog geen koers binnen.",
                    "price": None, "events": []}
        entry, atr = h["entry_price"], h["atr"] or h["entry_price"] * 0.03
        value = h["qty"] * price / fx
        pnl = value - h["amount_eur"]
        hw = max(h["high_water"] or price, price)
        stop, trail = h["stop"], h["trail_stop"]
        events: list[str] = []
        updates: dict = {"high_water": hw}
        t1_done = bool(h["t1_done"])
        if t1_done:
            trail = max(trail or 0, hw - TRAIL_ATR * atr, entry * (1 + BREAKEVEN_PCT / 100))
            updates["trail_stop"] = trail
        eff_stop = max(stop, trail or 0)
        base = {"price": price, "value_eur": value, "pnl_eur": pnl, "pnl_pct": pct(value, h["amount_eur"]),
                "stop": eff_stop, "trail_stop": trail, "target1": h["target1"], "target2": h["target2"],
                "to_stop_pct": pct(eff_stop, price), "to_t1_pct": pct(h["target1"], price),
                "to_t2_pct": pct(h["target2"], price), "price_at": price_at}
        if h["status"] == "closed":
            return {**base, "code": "closed", "title": f"Verkocht op {(h['closed_at'] or '')[:10]}",
                    "text": f"Resultaat {fmt_eur(pnl)} ({fpct(base['pnl_pct'])}).", "events": []}
        fresh = None
        try:
            fresh = self.recommender.plan_for(h["market"], h["ticker"])
        except Exception:  # noqa: BLE001
            fresh = None
        fp = (fresh or {}).get("plan") or {}
        momentum = fp.get("direction") == "long" and fp.get("conviction", 0) >= 50 and (fp.get("rsi") or 50) < 75

        if price <= eff_stop:
            if trail and eff_stop >= entry:
                code, title = "sell", "VERKOPEN: trailing stop geraakt"
                text = (f"De koers ({fmt_price(price)}) zakte onder de trailing stop ({fmt_price(eff_stop)}). Verkoop "
                        f"de rest en neem je winst: {fmt_eur(pnl)}.")
                events.append("trail")
            else:
                code, title = "sell", "VERKOPEN: stop-loss geraakt"
                text = (f"De koers ({fmt_price(price)}) is onder je stop-loss ({fmt_price(eff_stop)}). Verkoop om "
                        f"verder verlies te voorkomen (nu {fmt_eur(pnl)}).")
                events.append("stop")
        elif price >= h["target2"]:
            if momentum:
                code, title = "run", "Doel 2 gehaald: laat de rest lopen"
                trail = max(trail or 0, price - 1.5 * atr)
                updates["trail_stop"] = trail
                updates["t1_done"] = 1
                text = (f"Doel 2 ({fmt_price(h['target2'])}) is gehaald en het momentum is nog sterk. Laat de rest "
                        f"lopen; je krijgt een melding om te verkopen als de koers onder {fmt_price(trail)} zakt "
                        f"(trailing stop). Winst nu: {fmt_eur(pnl)}.")
                events.append("target2_run")
            else:
                code, title = "sell", "VERKOPEN: doel 2 gehaald"
                text = (f"Doel 2 ({fmt_price(h['target2'])}) is gehaald en het momentum neemt af. Verkoop nu en pak "
                        f"je winst: {fmt_eur(pnl)} ({fpct(base['pnl_pct'])}).")
                events.append("target2_sell")
        elif price >= h["target1"] and not t1_done:
            code, title = "partial", "DEELS VERKOPEN: doel 1 gehaald"
            be = entry * (1 + BREAKEVEN_PCT / 100)
            updates.update(t1_done=1, trail_stop=max(be, hw - TRAIL_ATR * atr))
            text = (f"Doel 1 ({fmt_price(h['target1'])}) is gehaald. Verkoop ongeveer de helft "
                    f"(~{fmt_eur(value / 2)}) en zet je stop op {fmt_price(updates['trail_stop'])}: de rest kan dan "
                    f"geen verlies meer worden en loopt door naar doel 2 ({fmt_price(h['target2'])}).")
            events.append("target1")
        else:
            code, title = "hold", "Houden"
            if t1_done:
                text = (f"Doel 1 is gehaald. De rest loopt door naar doel 2 ({fmt_price(h['target2'])}); verkoop als "
                        f"de koers onder de trailing stop van {fmt_price(eff_stop)} zakt.")
            else:
                text = (f"De koers ligt tussen je stop ({fmt_price(eff_stop)}, {fpct(base['to_stop_pct'])}) en "
                        f"doel 1 ({fmt_price(h['target1'])}, {fpct(base['to_t1_pct'])}).")
            # Waarschuwingen
            if fp.get("direction") == "short" and fp.get("conviction", 0) >= 40:
                code, title = "warn", "LET OP: de analyse slaat om naar dalen"
                text = (f"De technische analyse en de koopscore wijzen nu op een daling (overtuiging "
                        f"{fp['conviction']}). Overweeg te verkopen" + (" en je winst te nemen" if pnl > 0 else "")
                        + f". Resultaat nu {fmt_eur(pnl)}.")
                events.append("reversal")
            elif (fp.get("rsi") or 0) > 80 and base["pnl_pct"] > 8:
                code, title = "warn", "LET OP: oververhit, overweeg winst te nemen"
                text = (f"RSI {fp['rsi']:.0f}: de koers is erg hard gestegen. Overweeg (een deel van) je winst van "
                        f"{fmt_eur(pnl)} te nemen of je stop dichterbij te zetten.")
                events.append("reversal")
            else:
                bad = self.db.query_one(
                    "SELECT title FROM news WHERE market=? AND is_junk=0 AND tickers LIKE ? AND sentiment <= -0.4"
                    " AND corroboration >= 2 AND published_at >= ? ORDER BY credibility DESC LIMIT 1",
                    (h["market"], f'%"{h["ticker"]}"%', ago(hours=6)))
                if bad:
                    code, title = "warn", "LET OP: negatief nieuws"
                    text = f"{bad['title'][:160]}. Houd de stop in de gaten ({fmt_price(eff_stop)})."
                    events.append("news")
                elif utcnow() - (parse_iso(h["bought_at"]) or utcnow()) > timedelta(days=SLOW_DAYS):
                    code, title = "warn", "LET OP: het plan duurt lang"
                    text = (f"Na {SLOW_DAYS} dagen is doel 1 nog niet gehaald. Overweeg te verkopen en het geld in "
                            "een betere kans te zetten.")
                    events.append("slow")
        return {**base, **updates, "code": code, "title": title, "text": text, "events": events,
                "fresh": ({"direction": fp.get("direction"), "label": fp.get("label"),
                           "conviction": fp.get("conviction")} if fp else None)}

    def list(self, status: str = "open") -> list[dict]:
        rows = self.db.query("SELECT id FROM user_holdings WHERE status=? ORDER BY bought_at DESC", (status,))
        fx = self.rate()
        out = []
        for r in rows:
            h = self.get(r["id"])
            h["live"] = self.evaluate(h, fx)
            h["name"] = (self.db.query_one("SELECT name FROM price_meta WHERE market=? AND ticker=?",
                                           (h["market"], h["ticker"])) or {}).get("name")
            out.append(h)
        return out

    def totals(self, rows: list[dict]) -> dict:
        inv = sum(h["amount_eur"] for h in rows)
        val = sum((h["live"].get("value_eur") or h["amount_eur"]) for h in rows)
        return {"invested_eur": round(inv, 2), "value_eur": round(val, 2), "pnl_eur": round(val - inv, 2),
                "pnl_pct": round(pct(val, inv), 2) if inv else 0.0, "count": len(rows)}

    # ------------------------------------------------------------------ periodiek (elke 2 minuten)
    def run(self) -> int:
        fx = self.rate()
        n = 0
        for r in self.db.query("SELECT id FROM user_holdings WHERE status='open'"):
            h = self.get(r["id"])
            adv = self.evaluate(h, fx)
            upd = {k: adv[k] for k in ("high_water", "trail_stop", "t1_done") if k in adv and adv[k] is not None}
            notified = h["notified"]
            now = utcnow()
            for ev in adv.get("events", []):
                last = parse_iso(notified.get(ev))
                if last and (ev not in REPEAT or now - last < REPEAT[ev]):
                    continue
                self.notify(h, adv, ev)
                notified[ev] = iso(now)
                n += 1
            sets = ", ".join(f"{k}=?" for k in upd)
            self.db.execute(f"UPDATE user_holdings SET {sets + ', ' if sets else ''}advice=?, notified=? WHERE id=?",
                            (*upd.values(), json.dumps({k: adv.get(k) for k in ("code", "title", "text", "price")},
                                                       ensure_ascii=False), json.dumps(notified), h["id"]))
        self.publish()
        return n

    def notify(self, h: dict, adv: dict, event: str) -> None:
        if not (self.hass and self.cfg.notify_holdings):
            return
        kind = "alert" if event in SELL_EVENTS or event == "reversal" else "summary"
        title = f"{h['ticker']}: {adv['title']}"
        msg = f"{adv['text']} (koers {fmt_price(adv['price'])}, {MARKET_LABELS[h['market']].lower()})"
        try:
            self.hass.notify(title, msg, kind=kind, tag=f"market_monitor_holding_{h['id']}")
        except Exception as exc:  # noqa: BLE001
            log.warning("Melding positie mislukt: %s", exc)

    def publish(self) -> None:
        if not (self.hass and getattr(self.hass, "enabled", False)):
            return
        rows = self.list()
        t = self.totals(rows)
        self.hass.set_state("sensor.market_monitor_holdings", f"{t['value_eur']:.2f}", {
            "friendly_name": "Market Monitor: mijn posities",
            "icon": "mdi:briefcase-variant",
            "unit_of_measurement": "EUR",
            "ingelegd_eur": t["invested_eur"], "resultaat_eur": t["pnl_eur"], "resultaat_pct": t["pnl_pct"],
            "posities": [{"ticker": h["ticker"], "advies": h["live"].get("title"),
                          "resultaat_pct": round(h["live"].get("pnl_pct") or 0, 2)} for h in rows],
        })

    def tickers(self, market: str) -> list[str]:
        return [r["ticker"] for r in self.db.query(
            "SELECT DISTINCT ticker FROM user_holdings WHERE status='open' AND market=?", (market,))]
