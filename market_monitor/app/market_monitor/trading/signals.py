"""Handelssignaal per aandeel/munt: techniek + grote kopers + nieuws + X-posts, met jouw gewichten.

  ta    technische stemming (trend, MACD, RSI, steun/weerstand) uit technical.build_plan, -1..1
        in de stijl 'kort' gemengd met de koersbeweging van het laatste uur
  flow  grote kopers: insiders + politici (aandelen) of bedrijven + crypto-fondsen (crypto), -1..1
  news  sentiment van geloofwaardige berichten (inclusief de AI-analyse uit de samenvatting), -1..1
  x     sentiment van posts op X van de gevolgde accounts, laatste 12 uur, -1..1

edge = gewogen gemiddelde van de componenten waarvoor data is (de techniek telt altijd mee);
overtuiging = |edge| x 140, maximaal 100 (dezelfde schaal als de aanbeveling).
Daarnaast een handelsplan voor de kant van het signaal: stop-loss, twee doelen en risico/rendement vanaf de
huidige koers (want een order wordt direct uitgevoerd).
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone

from ..db import ago, parse_iso, utcnow
from ..scoring import fmt_price

FLOW_KEYS = {"stocks": {"insider": 0.6, "politician": 0.4}, "crypto": {"institutional": 1.0}}
SHORT_SCALE = 0.4          # stijl 'kort': stops/doelen op 0,4x de afstand van 'swing'
CHASE_ATR = 0.25           # niet najagen: maximaal 0,25x de dagbeweging buiten de instapzone


def clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def x_sentiment(db, market: str, hours: int = 12) -> dict[str, dict]:
    """Sentiment per ticker uit X-posts: gewogen naar geloofwaardigheid en actualiteit (halfwaardetijd 6 uur)."""
    now = utcnow()
    agg: dict[str, dict] = defaultdict(lambda: {"num": 0.0, "den": 0.0, "n": 0, "posts": []})
    rows = db.query(
        "SELECT author, title, credibility, sentiment, published_at, tickers FROM news WHERE market=? AND kind='post'"
        " AND is_junk=0 AND published_at >= ? AND tickers != '[]' ORDER BY published_at DESC LIMIT 400",
        (market, ago(hours=hours)),
    )
    for r in rows:
        age_h = max(0.0, (now - (parse_iso(r["published_at"]) or now)).total_seconds() / 3600)
        w = (r["credibility"] or 0.3) * 0.5 ** (age_h / 6)
        for t in json.loads(r["tickers"] or "[]"):
            a = agg[t]
            a["num"] += w * (r["sentiment"] or 0)
            a["den"] += w
            a["n"] += 1
            if len(a["posts"]) < 4:
                a["posts"].append({"door": r["author"], "tekst": (r["title"] or "")[:200], "tijd": r["published_at"]})
    out = {}
    for t, a in agg.items():
        raw = a["num"] / a["den"] if a["den"] else 0.0
        volume = min(1.0, a["n"] / 3)
        out[t] = {"score": clamp(raw * (0.5 + 0.5 * volume)), "n": a["n"], "posts": a["posts"]}
    return out


def short_momentum(market: str, meta: dict) -> float:
    """Koersbeweging van het laatste uur (crypto) of van vandaag (aandelen), -1..1."""
    if market == "crypto" and meta.get("chg_1h") is not None:
        return math.tanh(meta["chg_1h"] / 1.5)
    if meta.get("last_price") and meta.get("prev_close"):
        return math.tanh((meta["last_price"] / meta["prev_close"] - 1) * 100 / 2.0)
    return 0.0


def price_age_minutes(meta: dict, now: datetime | None = None) -> float | None:
    ts = parse_iso(meta.get("fetched_at"))
    if not ts:
        return None
    return ((now or utcnow()) - ts).total_seconds() / 60


def levels_for(side: str, price: float, plan: dict, settings: dict) -> dict | None:
    """Stop, doelen en risico/rendement voor een order die nu tegen 'price' wordt uitgevoerd."""
    atr = plan.get("atr") or price * 0.02
    kort = settings.get("trade_style") == "kort"
    scale = SHORT_SCALE if kort else 1.0
    a = atr * scale
    min_risk = settings["stop_atr_mult"] * a
    sign = 1 if side == "long" else -1
    use_plan = not kort and plan.get("direction") == side and plan.get("stop") and plan.get("target1")
    if use_plan:
        if side == "long":
            if plan.get("entry_high") and price > plan["entry_high"] + CHASE_ATR * atr:
                return {"skip": f"koers {fmt_price(price)} boven de instapzone (niet najagen)"}
            stop = min(plan["stop"], price - min_risk)
        else:
            if plan.get("entry_low") and price < plan["entry_low"] - CHASE_ATR * atr:
                return {"skip": f"koers {fmt_price(price)} onder de instapzone (niet najagen)"}
            stop = max(plan["stop"], price + min_risk)
        t1, t2 = plan["target1"], plan["target2"]
        if sign * (t1 - price) <= 0:          # doel 1 al gepasseerd: mik op doel 2
            t1 = t2
    else:
        stop = price - sign * min_risk
        risk = abs(price - stop)
        level = plan.get("resistance") if side == "long" else plan.get("support")
        far = max(2.0 * a, settings["min_rr"] * risk)
        if level and sign * (level - price) >= settings["min_rr"] * risk and abs(level - price) <= 3 * far:
            t1 = level
        else:
            t1 = price + sign * far
        t2 = price + sign * 2 * abs(t1 - price)
    risk = abs(price - stop)
    if risk <= 0 or sign * (t1 - price) <= 0:
        return {"skip": "geen geldig koersdoel"}
    if side == "short":
        t2 = max(t2, price * 0.05)
    rr = abs(t1 - price) / risk
    return {"stop": stop, "target1": t1, "target2": t2, "rr": round(rr, 2), "risk": risk, "atr": a,
            "plan_based": bool(use_plan)}


class SignalEngine:
    def __init__(self, db, recommender):
        self.db = db
        self.recommender = recommender

    def universe(self, market: str, settings: dict, extra: list[str] = ()) -> list[str]:
        rows = self.db.query("SELECT ticker, score FROM scores_latest WHERE market=?", (market,))
        rows.sort(key=lambda r: -abs(r["score"] - 50))
        allowed = set(settings.get("allowed_tickers") or [])
        blocked = set(settings.get("blocked_tickers") or [])
        tickers = [r["ticker"] for r in rows if (not allowed or r["ticker"] in allowed) and r["ticker"] not in blocked]
        tickers = tickers[: settings["universe_size"]]
        for t in extra:            # open posities altijd meenemen (ook als ze geblokkeerd zijn: dan alleen sluiten)
            if t not in tickers:
                tickers.append(t)
        return tickers

    def evaluate(self, market: str, settings: dict, extra: list[str] = (), now: datetime | None = None) -> list[dict]:
        now = now or utcnow()
        xs = x_sentiment(self.db, market)
        out = []
        for t in self.universe(market, settings, extra):
            sig = self.signal(market, t, settings, xs, now)
            if sig:
                out.append(sig)
        out.sort(key=lambda s: -s["conviction"])
        return out

    def signal(self, market: str, ticker: str, settings: dict, xs: dict | None = None,
               now: datetime | None = None) -> dict | None:
        try:
            p = self.recommender.plan_for(market, ticker)
        except Exception:  # noqa: BLE001 - rare koersdata: overslaan
            return None
        if not p:
            return None
        plan = p["plan"]
        meta = self.db.query_one("SELECT * FROM price_meta WHERE market=? AND ticker=?", (market, ticker)) or {}
        sc = self.db.query_one("SELECT components FROM scores_latest WHERE market=? AND ticker=?", (market, ticker))
        comps = json.loads(sc["components"]) if sc and sc.get("components") else {}
        xs = xs if xs is not None else x_sentiment(self.db, market)

        ta = plan["ta_score"]
        mom = short_momentum(market, meta)
        if settings.get("trade_style") == "kort":
            ta = clamp(0.6 * ta + 0.4 * mom)
        flow = sum(w * comps.get(k, 0.0) for k, w in FLOW_KEYS[market].items())
        news = comps.get("news", 0.0)
        x = (xs.get(ticker) or {}).get("score", 0.0)
        parts = {"ta": ta, "flow": flow, "news": news, "x": x}
        weights = {k: settings[f"weight_{k}"] for k in parts}
        used = {k: w for k, w in weights.items() if w > 0 and (k == "ta" or abs(parts[k]) > 0.01)}
        total_w = sum(used.values()) or 1.0
        edge = sum(parts[k] * w for k, w in used.items()) / total_w
        conviction = round(min(100.0, abs(edge) * 140))
        side = "long" if edge > 0 else "short"
        price = plan["price"]
        lv = levels_for(side, price, plan, settings)
        return {
            "market": market,
            "ticker": ticker,
            "name": p.get("name"),
            "price": price,
            "price_age_min": price_age_minutes(meta, now),
            "currency": p.get("currency") or "USD",
            "edge": round(edge, 3),
            "conviction": conviction,
            "side": side,
            "components": {k: round(v, 3) for k, v in parts.items()},
            "weights_used": used,
            "ta_direction": plan["direction"],
            "ta_conviction": plan["conviction"],
            "rsi": plan.get("rsi"),
            "atr": plan.get("atr"),
            "support": plan.get("support"),
            "resistance": plan.get("resistance"),
            "signals": plan.get("signals", [])[:6],
            "reasons": (p.get("reasons") or [])[:5],
            "x_posts": (xs.get(ticker) or {}).get("posts", []),
            "x_count": (xs.get(ticker) or {}).get("n", 0),
            "chg_1h": meta.get("chg_1h"),
            "chg_24h": meta.get("chg_24h"),
            "levels": lv,
            "plan": {k: plan.get(k) for k in ("direction", "entry_low", "entry_high", "stop", "target1", "target2",
                                               "rr", "atr", "support", "resistance", "price")},
            "score": p.get("score"),
        }


def stock_market_open(now: datetime) -> bool:
    """Reguliere handelstijd New York: ma-vr 9:30-16:00 (feestdagen niet meegerekend)."""
    from ..sources.prices import _ny_tz

    t = now.astimezone(_ny_tz())
    if t.weekday() >= 5:
        return False
    minutes = t.hour * 60 + t.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def market_open(market: str, settings: dict, now: datetime | None = None) -> bool:
    if market == "crypto" or not settings.get("stocks_market_hours_only", True):
        return True
    return stock_market_open(now or datetime.now(timezone.utc))
