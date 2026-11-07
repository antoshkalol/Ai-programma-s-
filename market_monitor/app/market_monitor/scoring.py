"""Koopscore (0-100), geschatte kans op stijging en instapmoment per aandeel.

Belangrijk: dit is een onderbouwde inschatting, geen voorspelling of beleggingsadvies.
Aandelen combineren vier signalen:
  * insiders  (30%): open-markt aankopen door bestuurders, extra gewicht als meerdere tegelijk kopen
  * politici  (20%): aankopen door Congresleden (met straf voor late melding)
  * nieuws    (25%): sentiment van geloofwaardige berichten, gewogen naar bron en actualiteit
  * koers     (25%): trend (50-daags gemiddelde), momentum en RSI -> ook het instapmoment
Crypto combineert drie signalen:
  * instituties (25%): bedrijven die munten kopen (SEC 8-K) en politici/insiders die crypto-ETF's kopen
  * nieuws      (35%)
  * koers       (40%): crypto beweegt sterker op momentum
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
from .markets import crypto_proxies, excluded_coins

log = logging.getLogger(__name__)

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


def fmt_num(v: float | None, decimals: int = 0) -> str:
    """Nederlandse notatie: 7.019 of 467,60."""
    if v is None:
        return "?"
    s = f"{v:,.{decimals}f}"
    return s.replace(",", "_").replace(".", ",").replace("_", ".")


def fmt_pct(v: float | None) -> str:
    return "?" if v is None else ("+" if v >= 0 else "-") + fmt_num(abs(v), 1) + "%"


def fmt_price(v: float | None) -> str:
    if v is None:
        return "?"
    if 0 < abs(v) < 1:
        decimals = max(2, -math.floor(math.log10(abs(v))) + 3)  # bv. 0,00001820
        return "$" + fmt_num(v, decimals)
    return "$" + fmt_num(v, 2)


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------- indicatoren
def sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def rsi(values: list[float], n: int = 14) -> float | None:
    """RSI volgens Wilder (zelfde berekening als in de technische analyse)."""
    from .technical import rsi_series

    if len(values) < n + 1:
        return None
    return rsi_series(values, n)[-1]


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


# --------------------------------------------------------------------------- crypto-componenten
def institutional_component(company: list[dict], proxy: list[dict]) -> tuple[float, list[str]]:
    """Bedrijven die de munt kopen (8-K) + politici/insiders die crypto-ETF's of -aandelen kopen."""
    reasons = []
    buy_v = sum(t["value_usd"] or 0 for t in company if t["tx_type"] == "buy")
    sell_v = sum(t["value_usd"] or 0 for t in company if t["tx_type"] == "sell")
    buyers = sorted({t["actor_name"] for t in company if t["tx_type"] == "buy"})
    raw = 0.5 * math.log10(1 + buy_v / 5e6) - 0.4 * math.log10(1 + sell_v / 5e6)
    if buyers:
        reasons.append(f"Bedrijven kochten voor {fmt_usd(buy_v)} in 30 dagen: {', '.join(buyers[:3])}")
    if sell_v:
        reasons.append(f"Bedrijven verkochten voor {fmt_usd(sell_v)}")
    pb = [t for t in proxy if t["tx_type"] == "buy"]
    ps = [t for t in proxy if t["tx_type"] == "sell"]
    if pb or ps:
        pv = sum(t["value_usd"] or 0 for t in pb)
        sv = sum(t["value_usd"] or 0 for t in ps)
        raw += 0.25 * math.log10(1 + pv / 50_000) - 0.15 * math.log10(1 + sv / 50_000)
        if pb:
            who = sorted({f"{t['actor_name']} ({t['ticker']})" for t in pb})[:3]
            reasons.append(f"Politici/insiders kochten crypto-fondsen of -aandelen (~{fmt_usd(pv)}): {', '.join(who)}")
        if ps:
            reasons.append(f"Politici/insiders verkochten crypto-fondsen of -aandelen (~{fmt_usd(sv)})")
    if not company and not proxy:
        return 0.0, reasons
    return clamp(math.tanh(raw)), reasons


# --------------------------------------------------------------------------- engine
class ScoringEngine:
    """Koopscore voor aandelen. CryptoScoringEngine hieronder hergebruikt dezelfde opbouw."""

    market = "stocks"
    weights = {"insider": 0.30, "politician": 0.20, "news": 0.25, "momentum": 0.25}
    signal_keys = ("insider", "politician", "news")
    base_rate = 0.53      # historisch stijgt een aandeel in iets meer dan de helft van de maanden
    period_word = "handelsdagen"

    def __init__(self, cfg, db: Database):
        self.cfg = cfg
        self.db = db

    def watchlist(self) -> list[str]:
        return list(self.cfg.watchlist(self.market))

    # ------------------------------------------------------------------ kandidaten
    def _news_counts(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for r in self.db.query(
            "SELECT tickers FROM news WHERE market=? AND is_junk=0 AND published_at >= ? AND tickers != '[]'",
            (self.market, ago(hours=24)),
        ):
            for t in json.loads(r["tickers"]):
                counts[t] += 1
        return counts

    @staticmethod
    def _clean(tickers: list[str], limit: int, excluded: set[str] = frozenset()) -> list[str]:
        clean = []
        for t in tickers:
            t = (t or "").upper().strip()
            if t and t not in clean and t not in excluded and len(t) <= 12:
                clean.append(t)
        return clean[:limit]

    def candidate_tickers(self, limit: int = 60) -> list[str]:
        out = self.watchlist()
        rows = self.db.query(
            "SELECT ticker, SUM(COALESCE(value_usd, 0)) v FROM trades WHERE market='stocks' AND tx_type='buy'"
            " AND ticker IS NOT NULL AND filed_at >= ? GROUP BY ticker ORDER BY v DESC LIMIT 40",
            (ago(days=30),),
        )
        out += [r["ticker"] for r in rows]
        out += [t for t, c in sorted(self._news_counts().items(), key=lambda x: -x[1]) if c >= 2][:20]
        return self._clean(out, limit)

    def probability(self, score: float, calibration: dict | None) -> tuple[float, str]:
        logit = math.log(self.base_rate / (1 - self.base_rate)) + PROB_SLOPE * (score - 50) / 50
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

    # ------------------------------------------------------------------ context en componenten
    def _news_and_llm(self) -> tuple[dict, dict]:
        news_by: dict[str, list[dict]] = defaultdict(list)
        for n in self.db.query(
            "SELECT tickers, credibility, sentiment, published_at FROM news WHERE market=? AND is_junk=0"
            " AND published_at >= ? AND tickers != '[]'",
            (self.market, ago(hours=48)),
        ):
            for t in json.loads(n["tickers"]):
                news_by[t].append(n)
        llm_by: dict[str, list[dict]] = defaultdict(list)
        for r in self.db.query(
            "SELECT ticker, sentiment, reason FROM ticker_sentiment WHERE market=? AND created_at >= ?"
            " ORDER BY created_at",
            (self.market, ago(hours=6)),
        ):
            llm_by[r["ticker"]].append(r)
        return news_by, llm_by

    def load_context(self, tickers: list[str]) -> dict:
        ph = ",".join("?" * len(tickers))
        trades = self.db.query(
            f"SELECT * FROM trades WHERE market='stocks' AND ticker IN ({ph}) AND filed_at >= ?"
            " AND tx_type IN ('buy','sell')",
            (*tickers, ago(days=60)),
        )
        by_t: dict[str, list[dict]] = defaultdict(list)
        for t in trades:
            by_t[t["ticker"]].append(t)
        news_by, llm_by = self._news_and_llm()
        return {"trades": by_t, "news": news_by, "llm": llm_by, "since30": ago(days=30)}

    def components(self, t: str, ctx: dict) -> tuple[dict, list[str]]:
        ins_trades = [x for x in ctx["trades"][t] if x["actor_type"] == "insider" and (x["filed_at"] or "") >= ctx["since30"]]
        pol_trades = [x for x in ctx["trades"][t] if x["actor_type"] == "politician"]
        ins, r1 = insider_component(ins_trades)
        pol, r2 = politician_component(pol_trades)
        nws, r3 = news_component(ctx["news"].get(t, []), ctx["llm"].get(t, []))
        return {"insider": ins, "politician": pol, "news": nws}, r1 + r2 + r3

    def price_reasons(self, ind: dict, meta: dict | None) -> list[str]:
        if not ind:
            return []
        trend = "boven" if ind.get("sma50") and ind["last"] > ind["sma50"] else "onder"
        rsi_txt = f", RSI {ind['rsi']:.0f}" if ind.get("rsi") is not None else ""
        return [f"Koers {trend} 50-daags gemiddelde, {fmt_pct(ind['ret20'] * 100)} in 20 {self.period_word}{rsi_txt}"]

    # ------------------------------------------------------------------ berekenen
    def run(self) -> int:
        tickers = self.candidate_tickers()
        if not tickers:
            return 0
        ctx = self.load_context(tickers)
        calibration = self.db.get_state(f"calibration_{self.market}")
        now = iso(utcnow())
        today = now[:10]
        watch = set(self.watchlist())
        results = 0
        for t in tickers:
            comps, reasons = self.components(t, ctx)
            closes = [r["close"] for r in self.db.query(
                "SELECT close FROM prices WHERE market=? AND ticker=? ORDER BY date DESC LIMIT 130",
                (self.market, t))][::-1]
            ind = indicators(closes)
            comps["momentum"] = momentum_component(ind)
            timing, _ = timing_of(ind)
            meta = self.db.query_one("SELECT * FROM price_meta WHERE market=? AND ticker=?", (self.market, t))
            reasons += self.price_reasons(ind, meta)
            total = sum(w * comps.get(k, 0.0) for k, w in self.weights.items())
            score = round(50 + 50 * clamp(total), 1)
            prob, src = self.probability(score, calibration)
            price = (meta or {}).get("last_price") or (closes[-1] if closes else None)
            has_signal = any(abs(comps.get(k, 0.0)) > 0.01 for k in self.signal_keys) or bool(ind)
            if not has_signal and t not in watch:
                continue
            self.db.execute(
                "INSERT INTO scores_latest(market, ticker, computed_at, score, prob_up, prob_source, timing, price,"
                " components, reasons) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(market, ticker) DO UPDATE SET"
                " computed_at=excluded.computed_at, score=excluded.score, prob_up=excluded.prob_up,"
                " prob_source=excluded.prob_source, timing=excluded.timing, price=excluded.price,"
                " components=excluded.components, reasons=excluded.reasons",
                (self.market, t, now, score, prob, src, timing, price,
                 json.dumps({k: round(v, 3) for k, v in comps.items()} | {"indicators": ind}),
                 json.dumps(reasons)),
            )
            if price:
                self.db.execute(
                    "INSERT OR IGNORE INTO score_history(market, ticker, day, score, price) VALUES (?,?,?,?,?)",
                    (self.market, t, today, score, price),
                )
            results += 1
        self.db.execute("DELETE FROM scores_latest WHERE market=? AND computed_at < ?", (self.market, ago(days=2)))
        return results

    def calibrate(self) -> int:
        """Meet hoe vaak eerdere scores uitkwamen (koers na ~20 koersdagen hoger?)."""
        cutoff = (utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
        rows = self.db.query(
            "SELECT ticker, day, score, price FROM score_history WHERE market=? AND day <= ?", (self.market, cutoff)
        )
        stats = {f"{lo}-{hi}": {"n": 0, "up": 0} for lo, hi in BUCKETS}
        for r in rows:
            later = self.db.query_one(
                "SELECT close FROM prices WHERE market=? AND ticker=? AND date > ? ORDER BY date LIMIT 1 OFFSET 19",
                (self.market, r["ticker"], r["day"]),
            )
            if not later or not r["price"]:
                continue
            for lo, hi in BUCKETS:
                if lo <= r["score"] < hi:
                    b = stats[f"{lo}-{hi}"]
                    b["n"] += 1
                    b["up"] += 1 if later["close"] > r["price"] else 0
        self.db.set_state(f"calibration_{self.market}", stats)
        return sum(b["n"] for b in stats.values())


class CryptoScoringEngine(ScoringEngine):
    market = "crypto"
    weights = {"institutional": 0.25, "news": 0.35, "momentum": 0.40}
    signal_keys = ("institutional", "news")
    base_rate = 0.52
    period_word = "dagen"

    def candidate_tickers(self, limit: int = 40) -> list[str]:
        excluded = excluded_coins()
        out = self.watchlist()
        rows = self.db.query(
            "SELECT ticker, SUM(COALESCE(value_usd, 0)) v FROM trades WHERE market='crypto' AND actor_type='company'"
            " AND ticker IS NOT NULL AND filed_at >= ? GROUP BY ticker ORDER BY v DESC LIMIT 10",
            (ago(days=30),),
        )
        out += [r["ticker"] for r in rows]
        known = set((self.db.get_state("coingecko_map") or {}).keys())
        out += [t for t, c in sorted(self._news_counts().items(), key=lambda x: -x[1])
                if c >= 2 and (not known or t in known)][:15]
        return self._clean(out, limit, excluded)

    def load_context(self, tickers: list[str]) -> dict:
        ph = ",".join("?" * len(tickers))
        company = self.db.query(
            f"SELECT * FROM trades WHERE market='crypto' AND actor_type='company' AND ticker IN ({ph})"
            " AND filed_at >= ?",
            (*tickers, ago(days=30)),
        )
        by_t: dict[str, list[dict]] = defaultdict(list)
        for t in company:
            by_t[t["ticker"]].append(t)
        proxies = {k: v for k, v in crypto_proxies().items() if v}
        proxy_by: dict[str, list[dict]] = defaultdict(list)
        if proxies:
            pph = ",".join("?" * len(proxies))
            for t in self.db.query(
                f"SELECT * FROM trades WHERE market='stocks' AND ticker IN ({pph}) AND filed_at >= ?"
                " AND tx_type IN ('buy','sell')",
                (*proxies, ago(days=60)),
            ):
                proxy_by[proxies[t["ticker"]]].append(t)
        news_by, llm_by = self._news_and_llm()
        return {"company": by_t, "proxy": proxy_by, "news": news_by, "llm": llm_by}

    def components(self, t: str, ctx: dict) -> tuple[dict, list[str]]:
        inst, r1 = institutional_component(ctx["company"].get(t, []), ctx["proxy"].get(t, []))
        nws, r2 = news_component(ctx["news"].get(t, []), ctx["llm"].get(t, []))
        return {"institutional": inst, "news": nws}, r1 + r2

    def price_reasons(self, ind: dict, meta: dict | None) -> list[str]:
        out = super().price_reasons(ind, meta)
        if meta and meta.get("chg_24h") is not None and meta.get("chg_7d") is not None:
            out.append(f"{fmt_pct(meta['chg_24h'])} in 24 uur, {fmt_pct(meta['chg_7d'])} in 7 dagen")
        return out


def engine_for(market: str, cfg, db: Database) -> ScoringEngine:
    return CryptoScoringEngine(cfg, db) if market == "crypto" else ScoringEngine(cfg, db)
