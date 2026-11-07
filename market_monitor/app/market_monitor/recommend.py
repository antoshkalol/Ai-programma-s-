"""Aanbeveling per markt: de kandidaat met het sterkste signaal (long of short) + technische analyse.

Kiest uit de aandelen/munten met de meest uitgesproken koopscore (ver van 50) degene waarbij model en
techniek het meest overtuigend dezelfde kant op wijzen, met een redelijke risico/rendement-verhouding.
Elke wisseling van aanbeveling wordt bewaard, zodat je later ziet hoe eerdere aanbevelingen uitpakten.
"""

from __future__ import annotations

import json
import logging

from .db import Database, iso, utcnow
from .technical import build_plan

log = logging.getLogger(__name__)

MIN_HISTORY = 60


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
            "score": score,
            "prob_up": prob,
            "prob_source": (sc or {}).get("prob_source", "model"),
            "timing": (sc or {}).get("timing"),
            "reasons": reasons or [],
        })
        return result

    @staticmethod
    def rank(p: dict) -> float:
        plan = p["plan"]
        rr = min(plan.get("rr") or 0, 3) / 3
        return plan["conviction"] * (0.7 + 0.3 * rr)

    # ------------------------------------------------------------------ beste kandidaat
    def update(self, market: str) -> dict | None:
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
        if not plans:
            return None
        directional = [p for p in plans if p["plan"]["direction"] != "neutral"]
        pick = max(directional, key=self.rank) if directional else max(plans, key=lambda p: p["score"])
        plan = pick["plan"]
        summary = {
            "ticker": pick["ticker"], "name": pick["name"], "direction": plan["direction"], "label": plan["label"],
            "action": plan["action"], "conviction": plan["conviction"], "price": plan["price"],
            "entry_low": plan["entry_low"], "entry_high": plan["entry_high"], "stop": plan["stop"],
            "target1": plan["target1"], "target2": plan["target2"], "rr": plan["rr"], "prob": plan["prob"],
            "signals": plan["signals"][:5], "updated_at": iso(utcnow()),
        }
        prev = self.db.get_state(f"recommendation_{market}") or {}
        summary["since"] = (prev.get("since") if prev.get("ticker") == summary["ticker"]
                            and prev.get("direction") == summary["direction"] else summary["updated_at"])
        self.db.set_state(f"recommendation_{market}", summary)
        last = self.db.query_one(
            "SELECT ticker, direction FROM recommendations WHERE market=? ORDER BY id DESC LIMIT 1", (market,)
        )
        if not last or (last["ticker"], last["direction"]) != (summary["ticker"], summary["direction"]):
            self.db.execute(
                "INSERT INTO recommendations(market, ticker, direction, created_at, price, entry, stop, target1,"
                " target2, plan) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (market, summary["ticker"], summary["direction"], summary["updated_at"], plan["price"], plan["entry"],
                 plan["stop"], plan["target1"], plan["target2"], json.dumps(summary, ensure_ascii=False)),
            )
        return pick

    def summary(self, market: str) -> dict | None:
        return self.db.get_state(f"recommendation_{market}")

    def current(self, market: str) -> dict | None:
        """Volledige aanbeveling met verse grafiekdata (voor het dashboard)."""
        s = self.summary(market)
        if not s:
            return None
        p = self.plan_for(market, s["ticker"])
        if p:
            p["since"] = s.get("since")
            p["updated_at"] = s.get("updated_at")
        return p

    # ------------------------------------------------------------------ geschiedenis
    def history(self, market: str, limit: int = 6) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM recommendations WHERE market=? ORDER BY id DESC LIMIT ?", (market, limit)
        )
        out = []
        for r in rows:
            closes = self.db.query(
                "SELECT date, close FROM prices WHERE market=? AND ticker=? AND date > ? ORDER BY date",
                (market, r["ticker"], r["created_at"][:10]),
            )
            meta = self.db.query_one("SELECT last_price FROM price_meta WHERE market=? AND ticker=?",
                                     (market, r["ticker"])) or {}
            now_price = meta.get("last_price") or (closes[-1]["close"] if closes else r["price"])
            status, ret = "loopt", None
            ref = r["entry"] or r["price"]
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
                        "price": r["price"], "entry": r["entry"], "now": now_price,
                        "return_pct": round(ret, 2) if ret is not None else None, "status": status})
        return out
