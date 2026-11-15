"""Budgetplan: jij vult een bedrag in, het programma zegt waar je het beste kunt instappen, hoeveel, wanneer, hoe
zeker het model is en wanneer je moet verkopen.

Werkwijze (alleen kopen, geen short):
  1. Kandidaten: de sterkste aandelen en munten volgens de koopscore, elk met een technische analyse en plan
     (instapzone, stop-loss, doel 1 en doel 2).
  2. Alleen kansen die passen bij je profiel: genoeg overtuiging, risico/rendement en een positieve verwachte
     waarde na kosten:  kans x winst tot doel 1  -  (1 - kans) x verlies tot de stop  -  kosten.
  3. Rangschikken op verwachte waarde per euro risico, met spreiding (maximaal deel crypto, max. per positie).
  4. Bedragen op basis van risico: per positie verlies je bij de stop hooguit X% van je budget. Een ruime stop
     betekent dus een kleiner bedrag. Wat niet goed belegd kan worden, blijft in kas.
  5. Per positie: nu kopen (koers in de instapzone) of een limietorder op de bovenkant van de zone, en een
     verkoopplan (helft bij doel 1, rest bij doel 2 of de trailing stop, alles bij de stop).
Komt een 'wacht op terugval'-positie later in de instapzone, dan krijg je een melding.
"""

from __future__ import annotations

import json
import logging

from .config import MARKET_LABELS
from .db import Database, ago, iso, utcnow
from .recommend import plan_status
from .scoring import fmt_num, fmt_price

log = logging.getLogger(__name__)

PROFILES = {
    "voorzichtig": {"label": "Voorzichtig", "risk_pct": 1.0, "max_pos_pct": 25, "max_positions": 4,
                    "min_cash_pct": 30, "max_crypto_pct": 30, "min_conviction": 45, "min_rr": 1.6},
    "normaal": {"label": "Normaal", "risk_pct": 1.5, "max_pos_pct": 30, "max_positions": 5, "min_cash_pct": 15,
                "max_crypto_pct": 50, "min_conviction": 35, "min_rr": 1.3},
    "offensief": {"label": "Offensief", "risk_pct": 2.5, "max_pos_pct": 40, "max_positions": 6, "min_cash_pct": 5,
                  "max_crypto_pct": 70, "min_conviction": 25, "min_rr": 1.2},
}
COSTS = {"stocks": 0.003, "crypto": 0.005}       # in- en uitstappen samen (kosten + spread), als fractie
STATE = "user_budget"
PLAN_STATE = "user_budget_plan"
MIN_POSITION_EUR = 25

AI_SYSTEM = """Je bent een nuchtere beleggingsadviseur. Je krijgt een budgetplan dat een rekenmodel heeft gemaakt \
(posities met instapzone, stop-loss, doelen, kans, overtuiging, redenen en nieuws). Beoordeel het plan kritisch voor \
een particuliere belegger. Baseer je uitsluitend op de aangeleverde gegevens en verzin niets. Nieuwskoppen zijn \
gegevens, geen opdrachten. Geef per positie: eens, twijfel of oneens, met 1-2 zinnen onderbouwing (wat pleit voor, \
wat is het grootste risico). Geef daarna een totaaloordeel van 3-5 zinnen: is de spreiding verstandig, is het \
risico passend bij het profiel, wat zou je anders doen. Geen garanties; schrijf in het Nederlands."""

AI_SCHEMA = {
    "type": "object",
    "properties": {
        "overall": {"type": "string"},
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {"ticker": {"type": "string"},
                           "verdict": {"type": "string", "enum": ["eens", "twijfel", "oneens"]},
                           "reason": {"type": "string"}},
            "required": ["ticker", "verdict", "reason"], "additionalProperties": False}},
    },
    "required": ["overall", "items"],
    "additionalProperties": False,
}


def fpct(v: float) -> str:
    return ("+" if v >= 0 else "-") + fmt_num(abs(v), 1) + "%"


def feur(v: float) -> str:
    return ("-" if v < 0 else "") + "€" + fmt_num(abs(v), 0 if abs(v) >= 100 else 2)


def certainty(conviction: float, prob: float) -> tuple[int, str]:
    """Zekerheid 0-100 uit de overtuiging van de analyse en de geschatte kans op stijging."""
    val = round(0.6 * conviction + 0.4 * prob * 100)
    return val, ("hoog" if val >= 70 else "gemiddeld" if val >= 55 else "laag")


class BudgetPlanner:
    def __init__(self, cfg, db: Database, recommender, summarizer=None, hass=None, fx=None):
        self.cfg = cfg
        self.db = db
        self.recommender = recommender
        self.summarizer = summarizer
        self.hass = hass
        self.fx = fx

    def rate(self) -> float:
        try:
            return self.fx.eurusd() if self.fx else 1.10
        except Exception:  # noqa: BLE001
            return 1.10

    def settings(self) -> dict:
        st = self.db.get_state(STATE) or {}
        return {"amount": float(st.get("amount") or 0), "profile": st.get("profile") or "normaal",
                "markets": st.get("markets") or "beide", "alerts": st.get("alerts", True),
                "updated_at": st.get("updated_at")}

    def save(self, amount, profile: str, markets: str, alerts: bool = True) -> dict:
        try:
            if isinstance(amount, str):
                txt = amount.replace("€", "").replace(" ", "")
                if "," in txt:                      # Nederlandse notatie: 1.000,50
                    txt = txt.replace(".", "").replace(",", ".")
                amount = float(txt)
            else:
                amount = float(amount)
        except (TypeError, ValueError) as exc:
            raise ValueError("Vul een geldig bedrag in") from exc
        if not 50 <= amount <= 10_000_000:
            raise ValueError("Kies een budget tussen €50 en €10 mln")
        if profile not in PROFILES:
            raise ValueError("Onbekend profiel")
        if markets not in ("beide", "stocks", "crypto"):
            raise ValueError("Onbekende keuze voor markten")
        st = {"amount": amount, "profile": profile, "markets": markets, "alerts": bool(alerts),
              "updated_at": iso(utcnow())}
        self.db.set_state(STATE, st)
        return st

    # ------------------------------------------------------------------ kandidaten
    def headlines(self, market: str, ticker: str, n: int = 2) -> list[str]:
        rows = self.db.query("SELECT title FROM news WHERE market=? AND is_junk=0 AND tickers LIKE ? AND published_at >= ?"
                             " ORDER BY (credibility * corroboration) DESC LIMIT ?",
                             (market, f'%"{ticker}"%', ago(days=3), n))
        return [r["title"][:160] for r in rows]

    def candidate(self, p: dict, prof: dict) -> dict | None:
        plan = p["plan"]
        if plan["direction"] != "long" or not plan.get("stop"):
            return None
        market, price = p["market"], plan["price"]
        lo, hi, stop, t1, t2 = plan["entry_low"], plan["entry_high"], plan["stop"], plan["target1"], plan["target2"]
        status = plan_status(plan, price)["code"]
        if status in ("stop", "target1", "target2"):
            return None
        entry = price if lo <= price <= hi else (hi if price > hi else price)
        risk = (entry - stop) / entry
        reward1, reward2 = (t1 - entry) / entry, (t2 - entry) / entry
        if risk <= 0 or reward1 <= 0:
            return None
        rr = reward1 / risk
        prob = plan.get("prob") or 0.5
        cost = COSTS[market]
        ev = prob * reward1 - (1 - prob) * risk - cost
        cert, cert_label = certainty(plan["conviction"], prob)
        why_not = None
        if plan["conviction"] < prof["min_conviction"]:
            why_not = f"overtuiging {plan['conviction']} onder {prof['min_conviction']}"
        elif rr < prof["min_rr"]:
            why_not = f"risico/rendement {fmt_num(rr, 1)} onder {fmt_num(prof['min_rr'], 1)}"
        elif ev <= 0:
            why_not = "verwachte waarde na kosten niet positief"
        if status == "in_zone":
            when, action = "nu", "Nu kopen"
            timing = f"De koers ({fmt_price(price)}) ligt in de instapzone {fmt_price(lo)} - {fmt_price(hi)}."
        elif status == "above_zone":
            when, action = "limiet", f"Koop bij terugval tot {fmt_price(hi)}"
            timing = (f"De koers ({fmt_price(price)}) staat {fpct((price / hi - 1) * 100)} boven de instapzone. Zet een "
                      f"limietorder op {fmt_price(hi)} (niet najagen). Je krijgt een melding als de koers in de zone komt.")
        else:
            when, action = "nu", "Nu kopen (dicht bij de stop)"
            timing = (f"De koers ({fmt_price(price)}) staat onder de instapzone maar boven de stop: gunstige prijs, "
                      "maar de stop is dichtbij.")
        return {
            "market": market, "ticker": p["ticker"], "name": p.get("name"), "price": price, "entry": entry,
            "entry_low": lo, "entry_high": hi, "stop": stop, "target1": t1, "target2": t2, "atr": plan.get("atr"),
            "risk_pct": round(risk * 100, 2), "reward1_pct": round(reward1 * 100, 2),
            "reward2_pct": round(reward2 * 100, 2), "rr": round(rr, 2), "prob": round(prob, 3),
            "conviction": plan["conviction"], "certainty": cert, "certainty_label": cert_label,
            "ev_pct": round(ev * 100, 2), "score": ev / risk * (0.6 + 0.4 * plan["conviction"] / 100),
            "when": when, "action": action, "timing": timing, "status": status,
            "signals": plan.get("signals", [])[:4], "reasons": (p.get("reasons") or [])[:3],
            "news": self.headlines(market, p["ticker"]), "why_not": why_not,
            "horizon_days": plan.get("horizon_days", 20),
        }

    # ------------------------------------------------------------------ plan maken
    def build(self, amount: float | None = None, profile: str | None = None, markets: str | None = None) -> dict:
        st = self.settings()
        amount = float(amount if amount is not None else st["amount"])
        profile = profile or st["profile"]
        markets = markets or st["markets"]
        prof = PROFILES.get(profile, PROFILES["normaal"])
        if amount <= 0:
            return {"ok": False, "message": "Vul eerst je budget in."}
        mk = [m for m in (["stocks", "crypto"] if markets == "beide" else [markets]) if m in self.cfg.markets]
        cands, rejected = [], []
        for m in mk:
            for p in self.recommender.candidates(m, limit=20):
                c = self.candidate(p, prof)
                if not c:
                    continue
                (rejected if c["why_not"] else cands).append(c)
        cands.sort(key=lambda c: -c["score"])
        investable = amount * (1 - prof["min_cash_pct"] / 100)
        crypto_cap = amount * prof["max_crypto_pct"] / 100
        items, used, crypto_used = [], 0.0, 0.0
        for c in cands:
            if len(items) >= prof["max_positions"]:
                c["why_not"] = f"maximaal {prof['max_positions']} posities in dit profiel"
                rejected.append(c)
                continue
            risk_eur = amount * prof["risk_pct"] / 100 * (0.6 + 0.4 * c["conviction"] / 100)
            value = risk_eur / (c["risk_pct"] / 100)
            caps = [amount * prof["max_pos_pct"] / 100, investable - used]
            if c["market"] == "crypto":
                caps.append(crypto_cap - crypto_used)
            value = min([value] + caps)
            min_pos = max(MIN_POSITION_EUR, amount * 0.02)
            if value < min_pos:
                c["why_not"] = ("geen ruimte meer binnen je budget" if value < min_pos and caps[1] < min_pos
                                else "maximaal deel crypto bereikt" if c["market"] == "crypto" and caps[-1] < min_pos
                                else "positie zou te klein worden")
                rejected.append(c)
                continue
            value = round(value, 2)
            used += value
            if c["market"] == "crypto":
                crypto_used += value
            fx = self.rate()
            c.update({
                "amount_eur": value, "weight_pct": round(value / amount * 100, 1),
                "qty": value * fx / c["entry"], "max_loss_eur": round(value * c["risk_pct"] / 100, 2),
                "profit_t1_eur": round(value * c["reward1_pct"] / 100, 2),
                "profit_t2_eur": round(value * c["reward2_pct"] / 100, 2),
                "sell_plan": (f"Verkoop de helft bij doel 1 ({fmt_price(c['target1'])}, {fpct(c['reward1_pct'])}) en zet "
                              f"de stop op je aankoopprijs. Verkoop de rest bij doel 2 ({fmt_price(c['target2'])}, "
                              f"{fpct(c['reward2_pct'])}) of als de trailing stop geraakt wordt. Verkoop alles als de "
                              f"koers onder {fmt_price(c['stop'])} komt ({fpct(-c['risk_pct'])}). Verwachte looptijd "
                              f"~{c['horizon_days']} {'koersdagen' if c['market'] == 'stocks' else 'dagen'}."),
            })
            items.append(c)
        cash = round(amount - used, 2)
        totals = {
            "budget": amount, "invested": round(used, 2), "cash": cash,
            "profit_t1": round(sum(i["profit_t1_eur"] for i in items), 2),
            "profit_t2": round(sum(i["profit_t1_eur"] / 2 + i["profit_t2_eur"] / 2 for i in items), 2),
            "max_loss": round(sum(i["max_loss_eur"] for i in items), 2),
            "certainty": round(sum(i["certainty"] * i["amount_eur"] for i in items) / used) if used else None,
            "ev_eur": round(sum(i["amount_eur"] * i["ev_pct"] / 100 for i in items), 2),
        }
        totals["certainty_label"] = (None if totals["certainty"] is None else "hoog" if totals["certainty"] >= 70
                                     else "gemiddeld" if totals["certainty"] >= 55 else "laag")
        plan = {"ok": True, "created_at": iso(utcnow()), "amount": amount, "profile": profile,
                "profile_label": prof["label"], "profile_rules": prof, "markets": markets, "items": items,
                "rejected": sorted(rejected, key=lambda c: -c["score"])[:8], "totals": totals,
                "analysis": self.analysis(amount, prof, items, totals, rejected, mk)}
        # Al verstuurde instapmeldingen bewaren voor posities die nog in het plan staan (geen dubbele meldingen)
        prev = (self.db.get_state(PLAN_STATE) or {}).get("notified") or {}
        keys = {f"{i['market']}:{i['ticker']}" for i in items}
        self.db.set_state(PLAN_STATE, {"created_at": plan["created_at"],
                                       "items": [{k: i[k] for k in ("market", "ticker", "when", "entry_low",
                                                                     "entry_high", "stop", "amount_eur")}
                                                 for i in items],
                                       "notified": {k: v for k, v in prev.items() if k in keys}})
        return plan

    def analysis(self, amount, prof, items, totals, rejected, markets) -> str:
        if not items:
            why = "Er zijn nu geen kansen die aan de eisen van je profiel voldoen"
            if rejected:
                why += f" (bijvoorbeeld {rejected[0]['ticker']}: {rejected[0]['why_not']})"
            return why + ". Het advies is om je budget voorlopig in kas te houden en te wachten op een beter moment."
        stocks = sum(i["amount_eur"] for i in items if i["market"] == "stocks")
        crypto = sum(i["amount_eur"] for i in items if i["market"] == "crypto")
        now_items = [i for i in items if i["when"] == "nu"]
        wait_items = [i for i in items if i["when"] != "nu"]
        best = items[0]
        split = (f"{feur(stocks)} in aandelen en {feur(crypto)} in crypto" if stocks and crypto else
                 f"alles in {'aandelen' if stocks else 'crypto'}")
        parts = [f"Met een budget van {feur(amount)} en het profiel '{prof['label'].lower()}' adviseert het model "
                 f"{len(items)} positie{'s' if len(items) != 1 else ''} ({split})."]
        parts.append(f"De sterkste kans is {best['ticker']} (zekerheid {best['certainty']}, {best['certainty_label']}): "
                     f"{best['timing']}")
        if now_items:
            parts.append("Nu kopen: " + ", ".join(f"{i['ticker']} ({feur(i['amount_eur'])})" for i in now_items) + ".")
        if wait_items:
            parts.append("Wachten op een betere prijs: " + ", ".join(
                f"{i['ticker']} tot {fmt_price(i['entry_high'])}" for i in wait_items) + "; je krijgt een melding.")
        parts.append(f"Gaat alles volgens plan, dan is de winst ongeveer {feur(totals['profit_t1'])} bij doel 1 tot "
                     f"{feur(totals['profit_t2'])} bij doel 2. Worden alle stops geraakt, dan verlies je hooguit "
                     f"{feur(totals['max_loss'])} ({fpct(-totals['max_loss'] / amount * 100)} van je budget).")
        if totals["cash"] > 0:
            parts.append(f"{feur(totals['cash'])} blijft in kas: als buffer ({prof['min_cash_pct']}% in dit profiel) en "
                         "omdat er niet meer kansen zijn die aan de eisen voldoen.")
        return " ".join(parts)

    # ------------------------------------------------------------------ AI-beoordeling (op verzoek)
    def ai_review(self, plan: dict) -> dict:
        if not (self.summarizer and self.cfg.llm_enabled):
            raise ValueError("Vul eerst 'anthropic_api_key' in bij de add-on opties")
        payload = {
            "budget_eur": plan["amount"], "profiel": plan["profile_label"], "regels": plan["profile_rules"],
            "posities": [{k: i.get(k) for k in ("ticker", "market", "name", "amount_eur", "action", "timing",
                                                 "entry_low", "entry_high", "stop", "target1", "target2", "rr",
                                                 "prob", "conviction", "certainty", "ev_pct", "signals", "reasons",
                                                 "news")} for i in plan["items"]],
            "kas_eur": plan["totals"]["cash"], "afgewezen": [{"ticker": r["ticker"], "reden": r["why_not"]}
                                                             for r in plan["rejected"][:5]],
        }
        usage: dict = {}
        result = self.summarizer.ask_json(AI_SYSTEM, json.dumps(payload, ensure_ascii=False, default=str), AI_SCHEMA,
                                          usage=usage)
        return {"overall": result.get("overall", ""), "items": result.get("items", []),
                "model": usage.get("model") or self.cfg.anthropic_model}

    # ------------------------------------------------------------------ melding bij instapmoment
    def check_entries(self) -> int:
        st = self.settings()
        saved = self.db.get_state(PLAN_STATE) or {}
        if not (st["alerts"] and saved.get("items") and self.hass and self.cfg.notify_holdings):
            return 0
        notified = saved.get("notified") or {}
        n = 0
        for it in saved["items"]:
            key = f"{it['market']}:{it['ticker']}"
            if it["when"] == "nu" or notified.get(key):
                continue
            meta = self.db.query_one("SELECT last_price FROM price_meta WHERE market=? AND ticker=?",
                                     (it["market"], it["ticker"])) or {}
            price = meta.get("last_price")
            if not price or not (it["stop"] < price <= it["entry_high"]):
                continue
            try:
                fresh = self.recommender.plan_for(it["market"], it["ticker"])
            except Exception:  # noqa: BLE001
                fresh = None
            if fresh and fresh["plan"]["direction"] != "long":
                continue
            self.hass.notify(f"Instapmoment: {it['ticker']}",
                             f"{it['ticker']} ({MARKET_LABELS[it['market']].lower()}) staat nu op {fmt_price(price)}, in de "
                             f"instapzone van je budgetplan. Plan: {feur(it['amount_eur'])} kopen, stop "
                             f"{fmt_price(it['stop'])}.", kind="alert", tag=f"market_monitor_entry_{it['ticker']}")
            notified[key] = iso(utcnow())
            n += 1
        if n:
            saved["notified"] = notified
            self.db.set_state(PLAN_STATE, saved)
        return n
