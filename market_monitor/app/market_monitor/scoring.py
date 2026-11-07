"""Koopscore (0-100), geschatte kans op stijging en instapmoment per aandeel.

Belangrijk: dit is een onderbouwde inschatting, geen voorspelling of beleggingsadvies.
De score combineert vier signalen:
  * insiders  (30%): open-markt aankopen door bestuurders, extra gewicht als meerdere tegelijk kopen
  * politici  (20%): aankopen door Congresleden (met straf voor late melding)
  * nieuws    (25%): sentiment van geloofwaardige berichten, gewogen naar bron en actualiteit
  * koers     (25%): trend (50-daags gemiddelde), momentum en RSI -> ook het instapmoment
De 'kans omhoog' (horizon ~1 maand) start als modelschatting en wordt automatisch
gekalibreerd zodra er genoeg historie is (de app meet hoe vaak eerdere scores uitkwamen).
"""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from datetime import timedelta

from .db import Database, ago, iso, parse_iso, utcnow

log = logging.getLogger(__name__)

WEIGHTS = {"insider": 0.30, "politician": 0.20, "news": 0.25, "momentum": 0.25}
BASE_RATE = 0.53          # historisch: iets meer dan de helft van de maanden stijgt een aandeel
PROB_SLOPE = 1.2
PROB_MIN, PROB_MAX = 0.25, 0.80
BUCKETS = [(0, 40), (40, 50), (50, 60), (60, 70), (70, 101)]
MIN_CALIBRATION_N = 30
SENIOR_TITLES = ("ceo", "chief executive", "cfo", "chief financial", "president", "chair", "coo", "founder")


# --------------------------------------------------------------------------- opmaak
def fmt_usd(v: float | None) -> str:
    if v is None:
        return "onbekend bedrag"
    if v >= 1e9:
        return f"${v / 1e9:.1f} mld".replace(".", ",")
    if v >= 1e6:
        return f"${v / 1e6:.1f} mln".replace(".", ",")
    if v >= 1e3:
        return f"${v / 1e3:.0f}k"
    return f"${v:.0f}"


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------- indicatoren
def sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def rsi(values: list[float], n: int = 14) -> float | None:
    if len(values) < n + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-n, 0):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)


def indicators(closes: list[float]) -> dict:
    if len(closes) < 22:
        return {}
    last = closes[-1]
    s20, s50 = sma(closes, 20), sma(closes, 50)
    high20 = max(closes[-20:])
    ret20 = last / closes[-21] - 1
    rets = [closes[i] / closes[i - 1] - 1 for i in range(-20, 0)]
    mean = sum(rets) / len(rets)
    vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets)) * math.sqrt(252)
    return {
        "last": last,
        "sma20": s20,
        "sma50": s50,
        "rsi": rsi(closes),
        "ret20": ret20,
        "pullback": 1 - last / high20 if high20 else 0.0,
        "volatility": vol,
    }


def timing_of(ind: dict) -> tuple[str, float]:
    """Geeft (label, score) voor het instapmoment."""
    if not ind:
        return "Geen koersdata", 0.0
    r, last, s20, s50 = ind.get("rsi"), ind["last"], ind.get("sma20"), ind.get("sma50")
    if r is not None and r > 72:
        return "Oververhit: wacht op terugval", -0.3
    if s50 and s20 and last < s50 and s20 < s50:
        return "Dalende trend: afwachten", -0.4
    if s50 and last > s50 and r is not None and 38 <= r <= 65 and 0.02 <= ind["pullback"] <= 0.12:
        return "Gunstig: terugval in stijgende trend", 0.35
    if s50 and last > s50:
        return "Stijgende trend", 0.15
    return "Neutraal", 0.0


def momentum_component(ind: dict) -> float:
    if not ind:
        return 0.0
    s20, s50, last = ind.get("sma20"), ind.get("sma50"), ind["last"]
    trend = 0.0
    if s50 and s20:
        if last > s50 and s20 > s50:
            trend = 1.0
        elif last < s50 and s20 < s50:
            trend = -1.0
    _, tscore = timing_of(ind)
    return clamp(0.4 * trend + 0.4 * math.tanh(ind["ret20"] * 5) + 0.5 * tscore)


# --------------------------------------------------------------------------- componenten
def insider_component(trades: list[dict]) -> tuple[float, list[str]]:
    buys = [t for t in trades if t["tx_type"] == "buy"]
    sells = [t for t in trades if t["tx_type"] == "sell"]
    buy_value = sum((t["value_usd"] or 0) * (0.5 if t["planned"] else 1.0) for t in buys)
    sell_value = sum((t["value_usd"] or 0) * (0.3 if t["planned"] else 1.0) for t in sells)
    buyers = {t["actor_name"] for t in buys}
    senior = any(any(s in (t["actor_role"] or "").lower() for s in SENIOR_TITLES) for t in buys)
    reasons = []
    if not buys and not sells:
        return 0.0, reasons
    buy_strength = math.log10(1 + buy_value / 10_000)
    cluster = min(2.0, 1 + 0.35 * max(0, len(buyers) - 1)) * (1.25 if senior else 1.0)
    sell_strength = math.log10(1 + sell_value / 100_000) * 0.3
    score = clamp(math.tanh(0.6 * buy_strength * cluster - 0.5 * sell_strength))
    if buys:
        who = ", ".join(sorted(buyers)[:2]) + (f" +{len(buyers) - 2}" if len(buyers) > 2 else "")
        extra = " (o.a. topbestuurder)" if senior else ""
        reasons.append(f"{len(buyers)} insider(s) kochten {fmt_usd(buy_value)} in 30 dagen{extra}: {who}")
    raw_sell = sum(t["value_usd"] or 0 for t in sells)
    if sells and raw_sell > 0:
        planned = " (deels via vooraf ingepland 10b5-1 plan)" if any(t["planned"] for t in sells) else ""
        reasons.append(f"Insiders verkochten {fmt_usd(raw_sell)}{planned}; verkopen zeggen minder dan kopen")
    return score, reasons


def politician_component(trades: list[dict]) -> tuple[float, list[str]]:
    buys = [t for t in trades if t["tx_type"] == "buy"]
    sells = [t for t in trades if t["tx_type"] == "sell"]
    if not buys and not sells:
        return 0.0, []
    buy_mid = sum(t["value_usd"] or 0 for t in buys)
    sell_mid = sum(t["value_usd"] or 0 for t in sells)
    pols = {t["actor_name"] for t in buys}
    delays = []
    for t in trades:
        td, fd = parse_iso(t.get("tx_date")), parse_iso(t.get("filed_at"))
        if td and fd:
            delays.append(max(0, (fd - td).days))
    delay = sum(delays) / len(delays) if delays else 30
    factor = max(0.4, 1 - delay / 90)
    raw = 0.5 * math.log10(1 + buy_mid / 15_000) * (1 + 0.3 * max(0, len(pols) - 1)) - 0.25 * math.log10(
        1 + sell_mid / 15_000
    )
    score = clamp(math.tanh(raw) * factor)
    reasons = []
    if buys:
        reasons.append(
            f"{len(pols)} politicus/politici kochten (~{fmt_usd(buy_mid)}): {', '.join(sorted(pols)[:3])}"
            f" - gemiddeld {delay:.0f} dagen na de transactie gemeld"
        )
    if sells:
        reasons.append(f"Politici verkochten (~{fmt_usd(sell_mid)})")
    return score, reasons


def news_component(news: list[dict], llm: list[dict]) -> tuple[float, list[str]]:
    now = utcnow()
    num, den = 0.0, 0.0
    for n in news:
        pub = parse_iso(n["published_at"]) or now
        age_h = max(0.0, (now - pub).total_seconds() / 3600)
        w = (n["credibility"] or 0.3) * math.exp(-age_h / 24)
        num += w * (n["sentiment"] or 0)
        den += w
    lex = num / den if den else 0.0
    volume = min(1.0, len(news) / 5)
    lex_score = lex * (0.5 + 0.5 * volume) if news else 0.0
    reasons = []
    if llm:
        ai = sum(x["sentiment"] for x in llm) / len(llm)
        score = clamp(0.4 * lex_score + 0.6 * ai)
        last_reason = llm[-1].get("reason")
        if last_reason:
            reasons.append(f"AI-analyse nieuws: {last_reason}")
    else:
        score = clamp(lex_score)
    if news:
        mood = "positief" if score > 0.15 else "negatief" if score < -0.15 else "gemengd/neutraal"
        reasons.append(f"Nieuws {mood} ({len(news)} geloofwaardige bericht(en), 48 uur)")
    return score, reasons


# --------------------------------------------------------------------------- engine
class ScoringEngine:
    def __init__(self, cfg, db: Database):
        self.cfg = cfg
        self.db = db

    def candidate_tickers(self, limit: int = 60) -> list[str]:
        out: list[str] = list(dict.fromkeys(self.cfg.watchlist))
        rows = self.db.query(
            "SELECT ticker, SUM(COALESCE(value_usd, 0)) v FROM trades WHERE tx_type='buy' AND ticker IS NOT NULL "
            "AND filed_at >= ? GROUP BY ticker ORDER BY v DESC LIMIT 40",
            (ago(days=30),),
        )
        out += [r["ticker"] for r in rows]
        news_counts: dict[str, int] = defaultdict(int)
        for r in self.db.query(
            "SELECT tickers FROM news WHERE is_junk=0 AND published_at >= ? AND tickers != '[]'", (ago(hours=24),)
        ):
            for t in json.loads(r["tickers"]):
                news_counts[t] += 1
        out += [t for t, c in sorted(news_counts.items(), key=lambda x: -x[1]) if c >= 2][:20]
        clean = []
        for t in out:
            t = (t or "").upper().strip()
            if t and t not in clean and len(t) <= 12:
                clean.append(t)
        return clean[:limit]

    def probability(self, score: float, calibration: dict | None) -> tuple[float, str]:
        logit = math.log(BASE_RATE / (1 - BASE_RATE)) + PROB_SLOPE * (score - 50) / 50
        p_model = 1 / (1 + math.exp(-logit))
        p_model = max(PROB_MIN, min(PROB_MAX, p_model))
        if calibration:
            for lo, hi in BUCKETS:
                if lo <= score < hi:
                    b = calibration.get(f"{lo}-{hi}")
                    if b and b["n"] >= MIN_CALIBRATION_N:
                        alpha = 20
                        p = (b["up"] + alpha * p_model) / (b["n"] + alpha)
                        return round(p, 3), "gekalibreerd"
        return round(p_model, 3), "model"

    def run(self) -> int:
        tickers = self.candidate_tickers()
        if not tickers:
            return 0
        since30 = ago(days=30)
        ph = ",".join("?" * len(tickers))
        trades = self.db.query(
            f"SELECT * FROM trades WHERE ticker IN ({ph}) AND filed_at >= ? AND tx_type IN ('buy','sell')",
            (*tickers, ago(days=60)),
        )
        by_t: dict[str, list[dict]] = defaultdict(list)
        for t in trades:
            by_t[t["ticker"]].append(t)
        news_rows = self.db.query(
            "SELECT tickers, credibility, sentiment, published_at FROM news WHERE is_junk=0 AND published_at >= ?"
            " AND tickers != '[]'",
            (ago(hours=48),),
        )
        news_by: dict[str, list[dict]] = defaultdict(list)
        for n in news_rows:
            for t in json.loads(n["tickers"]):
                news_by[t].append(n)
        llm_by: dict[str, list[dict]] = defaultdict(list)
        for r in self.db.query(
            "SELECT ticker, sentiment, reason FROM ticker_sentiment WHERE created_at >= ? ORDER BY created_at",
            (ago(hours=6),),
        ):
            llm_by[r["ticker"]].append(r)
        calibration = self.db.get_state("calibration")
        now = iso(utcnow())
        today = now[:10]

        results = 0
        for t in tickers:
            ins_trades = [x for x in by_t[t] if x["actor_type"] == "insider" and (x["filed_at"] or "") >= since30]
            pol_trades = [x for x in by_t[t] if x["actor_type"] == "politician"]
            ins, r1 = insider_component(ins_trades)
            pol, r2 = politician_component(pol_trades)
            nws, r3 = news_component(news_by.get(t, []), llm_by.get(t, []))
            closes = [r["close"] for r in self.db.query(
                "SELECT close FROM prices WHERE ticker=? ORDER BY date DESC LIMIT 130", (t,))][::-1]
            ind = indicators(closes)
            mom = momentum_component(ind)
            timing, _ = timing_of(ind)
            r4 = []
            if ind:
                trend = "boven" if ind.get("sma50") and ind["last"] > ind["sma50"] else "onder"
                rsi_txt = f", RSI {ind['rsi']:.0f}" if ind.get("rsi") is not None else ""
                r4.append(f"Koers {trend} 50-daags gemiddelde, {ind['ret20'] * 100:+.1f}% in 20 handelsdagen{rsi_txt}")
            comps = {"insider": ins, "politician": pol, "news": nws, "momentum": mom}
            total = sum(WEIGHTS[k] * v for k, v in comps.items())
            score = round(50 + 50 * clamp(total), 1)
            prob, src = self.probability(score, calibration)
            meta = self.db.query_one("SELECT last_price FROM price_meta WHERE ticker=?", (t,))
            price = (meta or {}).get("last_price") or (closes[-1] if closes else None)
            has_signal = any(abs(v) > 0.01 for v in (ins, pol, nws)) or bool(ind)
            if not has_signal and t not in self.cfg.watchlist:
                continue
            self.db.execute(
                "INSERT INTO scores_latest(ticker, computed_at, score, prob_up, prob_source, timing, price, components,"
                " reasons) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET computed_at=excluded.computed_at,"
                " score=excluded.score, prob_up=excluded.prob_up, prob_source=excluded.prob_source,"
                " timing=excluded.timing, price=excluded.price, components=excluded.components, reasons=excluded.reasons",
                (t, now, score, prob, src, timing, price,
                 json.dumps({k: round(v, 3) for k, v in comps.items()} | {"indicators": ind}),
                 json.dumps(r1 + r2 + r3 + r4)),
            )
            if price:
                self.db.execute(
                    "INSERT OR IGNORE INTO score_history(ticker, day, score, price) VALUES (?,?,?,?)",
                    (t, today, score, price),
                )
            results += 1
        # Oude tickers zonder recente berekening opruimen
        self.db.execute("DELETE FROM scores_latest WHERE computed_at < ?", (ago(days=2),))
        return results

    def calibrate(self) -> int:
        """Meet hoe vaak eerdere scores uitkwamen (koers na ~20 handelsdagen hoger?)."""
        cutoff = (utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
        rows = self.db.query("SELECT ticker, day, score, price FROM score_history WHERE day <= ?", (cutoff,))
        stats = {f"{lo}-{hi}": {"n": 0, "up": 0} for lo, hi in BUCKETS}
        for r in rows:
            later = self.db.query_one(
                "SELECT close FROM prices WHERE ticker=? AND date > ? ORDER BY date LIMIT 1 OFFSET 19",
                (r["ticker"], r["day"]),
            )
            if not later or not r["price"]:
                continue
            for lo, hi in BUCKETS:
                if lo <= r["score"] < hi:
                    b = stats[f"{lo}-{hi}"]
                    b["n"] += 1
                    b["up"] += 1 if later["close"] > r["price"] else 0
        self.db.set_state("calibration", stats)
        return sum(b["n"] for b in stats.values())
