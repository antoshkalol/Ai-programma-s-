"""Technische analyse op dagslotkoersen en een handelsplan (long / short / afwachten).

Gebruikt alleen slotkoersen (die hebben we voor zowel aandelen als crypto):
  * trend      : SMA20, SMA50, regressiekanaal over 60 dagen (helling en betrouwbaarheid R²)
  * momentum   : MACD (12/26/9) en RSI (14)
  * volatiliteit: Bollinger-banden (20, 2σ), gemiddelde dagbeweging (ATR-benadering), dagelijkse σ
  * niveaus    : steun en weerstand uit 'swing'-toppen en -bodems die meerdere keren getest zijn

De richting combineert de koopscore van het model (60%) met de technische stemming (40%).
Het plan geeft een instapzone, stop-loss, twee koersdoelen, risico/rendement en een verwachtingskegel
(±1σ) voor de komende ~20 koersdagen. Het is een schets van een scenario, geen voorspelling.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

HORIZON = 20


# --------------------------------------------------------------------------- indicatoren
def sma_series(v: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(v)
    s = 0.0
    for i, x in enumerate(v):
        s += x
        if i >= n:
            s -= v[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema_series(v: list[float], n: int) -> list[float]:
    k = 2 / (n + 1)
    out, e = [], None
    for x in v:
        e = x if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def rsi_series(v: list[float], n: int = 14) -> list[float | None]:
    """RSI volgens Wilder."""
    out: list[float | None] = [None] * len(v)
    if len(v) <= n:
        return out
    gains = sum(max(v[i] - v[i - 1], 0) for i in range(1, n + 1))
    losses = sum(max(v[i - 1] - v[i], 0) for i in range(1, n + 1))
    ag, al = gains / n, losses / n

    def val(g: float, lo: float) -> float:
        return 100.0 if lo == 0 else 100 - 100 / (1 + g / lo)

    out[n] = val(ag, al)
    for i in range(n + 1, len(v)):
        d = v[i] - v[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = val(ag, al)
    return out


def macd(v: list[float]) -> tuple[list[float], list[float], list[float]]:
    line = [a - b for a, b in zip(ema_series(v, 12), ema_series(v, 26))]
    signal = ema_series(line, 9)
    return line, signal, [a - b for a, b in zip(line, signal)]


def bollinger(v: list[float], n: int = 20, k: float = 2.0) -> tuple[list, list, list]:
    mid = sma_series(v, n)
    up: list[float | None] = [None] * len(v)
    low: list[float | None] = [None] * len(v)
    for i, m in enumerate(mid):
        if m is None:
            continue
        window = v[i - n + 1: i + 1]
        sd = math.sqrt(sum((x - m) ** 2 for x in window) / n)
        up[i], low[i] = m + k * sd, m - k * sd
    return mid, up, low


def regression(v: list[float], lookback: int = 60) -> dict:
    """Lineaire regressie op log-koersen: helling (per dag), R² en spreiding van de residuen."""
    seg = v[-lookback:]
    m = len(seg)
    ys = [math.log(x) for x in seg]
    xs = list(range(m))
    mx, my = sum(xs) / m, sum(ys) / m
    sxx = sum((x - mx) ** 2 for x in xs) or 1.0
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    intercept = my - slope * mx
    resid = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
    ss_res = sum(r * r for r in resid)
    ss_tot = sum((y - my) ** 2 for y in ys) or 1e-12
    return {
        "slope": slope,
        "intercept": intercept,
        "start": len(v) - m,
        "sigma": math.sqrt(ss_res / max(1, m - 2)),
        "r2": max(0.0, 1 - ss_res / ss_tot),
        "length": m,
    }


def swing_levels(v: list[float], window: int = 5, lookback: int = 120, tol: float = 0.015) -> list[dict]:
    """Steun-/weerstandsniveaus: clusters van lokale toppen en bodems."""
    start = max(window, len(v) - lookback)
    points = []
    for i in range(start, len(v) - window):
        neigh = v[i - window: i + window + 1]
        if v[i] == max(neigh) or v[i] == min(neigh):
            points.append((v[i], i))
    points.sort()
    levels: list[dict] = []
    for price, i in points:
        if levels and abs(price - levels[-1]["price"]) / levels[-1]["price"] <= tol:
            lv = levels[-1]
            lv["touches"] += 1
            lv["price"] = lv["price"] + (price - lv["price"]) / lv["touches"]
            lv["last"] = max(lv["last"], i)
        else:
            levels.append({"price": price, "touches": 1, "last": i})
    return levels


def avg_move(v: list[float], n: int = 14) -> float:
    """Gemiddelde absolute dagbeweging (benadering van ATR met alleen slotkoersen)."""
    diffs = [abs(v[i] - v[i - 1]) for i in range(max(1, len(v) - n), len(v))]
    return sum(diffs) / len(diffs) if diffs else 0.0


def daily_sigma(v: list[float], n: int = 30) -> float:
    rets = [math.log(v[i] / v[i - 1]) for i in range(max(1, len(v) - n), len(v)) if v[i - 1] > 0]
    if len(rets) < 2:
        return 0.0
    m = sum(rets) / len(rets)
    return math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))


def future_dates(last: str, n: int, weekdays_only: bool) -> list[str]:
    d = date.fromisoformat(last)
    out = []
    while len(out) < n:
        d += timedelta(days=1)
        if weekdays_only and d.weekday() >= 5:
            continue
        out.append(d.isoformat())
    return out


def _fmt(v: float) -> str:
    if v is None:
        return "?"
    if abs(v) < 1:
        decimals = max(2, -math.floor(math.log10(abs(v))) + 3) if v else 2
        return "$" + f"{v:.{decimals}f}".replace(".", ",")
    s = f"{v:,.2f}" if abs(v) < 1000 else f"{v:,.0f}"
    return "$" + s.replace(",", "_").replace(".", ",").replace("_", ".")


def _pct(v: float) -> str:
    return ("+" if v >= 0 else "-") + f"{abs(v):.1f}".replace(".", ",") + "%"


# --------------------------------------------------------------------------- plan
def build_plan(dates: list[str], closes: list[float], score: float = 50.0, prob_up: float = 0.5,
               market: str = "stocks", horizon: int = HORIZON) -> dict:
    """Maakt de technische analyse + handelsplan + grafiekdata voor één aandeel/munt."""
    n = len(closes)
    if n < 60:
        raise ValueError("minstens 60 slotkoersen nodig")
    price = closes[-1]
    sma20, sma50 = sma_series(closes, 20), sma_series(closes, 50)
    rsi = rsi_series(closes)
    m_line, m_sig, m_hist = macd(closes)
    bb_mid, bb_up, bb_low = bollinger(closes)
    reg = regression(closes, 60)
    atr = avg_move(closes) or price * 0.01
    sigma = daily_sigma(closes) or 0.01
    levels = swing_levels(closes)

    # Dichtstbijzijnde steun/weerstand (anders recente bodem/top)
    supports = sorted([lv for lv in levels if lv["price"] < price * 0.997], key=lambda lv: -lv["price"])
    resistances = sorted([lv for lv in levels if lv["price"] > price * 1.003], key=lambda lv: lv["price"])
    support = supports[0]["price"] if supports else (min(closes[-60:]) if min(closes[-60:]) < price else None)
    resistance = resistances[0]["price"] if resistances else (max(closes[-60:]) if max(closes[-60:]) > price else None)

    # ------------------------------------------------------------ technische stemming
    r_now, s20, s50 = rsi[-1], sma20[-1], sma50[-1]
    month_trend = math.exp(reg["slope"] * 21) - 1
    votes, signals = 0.0, []
    if s50:
        above = price > s50
        votes += 0.25 if above else -0.25
        signals.append(f"Koers {'boven' if above else 'onder'} het 50-daags gemiddelde ({_fmt(s50)})")
    if s20 and s50:
        up = s20 > s50
        votes += 0.2 if up else -0.2
        signals.append(f"Korte trend (SMA20) {'boven' if up else 'onder'} lange trend (SMA50)")
    trend_weight = min(1.0, reg["r2"] * 2)
    votes += (0.25 if reg["slope"] > 0 else -0.25) * trend_weight
    signals.append(f"Trendkanaal (60 dagen) {'stijgend' if reg['slope'] > 0 else 'dalend'}: ongeveer {_pct(month_trend * 100)} "
                   f"per maand, betrouwbaarheid R² {reg['r2']:.2f}".replace(".", ","))
    macd_up = m_hist[-1] > 0
    votes += 0.15 if macd_up else -0.15
    signals.append(f"MACD {'boven' if macd_up else 'onder'} de signaallijn: momentum {'positief' if macd_up else 'negatief'}")
    if r_now is not None:
        if r_now > 70:
            votes -= 0.15
            signals.append(f"RSI {r_now:.0f}: oververhit (kans op terugval)")
        elif r_now < 30:
            votes += 0.15
            signals.append(f"RSI {r_now:.0f}: oversold (kans op herstel)")
        else:
            signals.append(f"RSI {r_now:.0f}: neutraal")
    if bb_up[-1] and price > bb_up[-1]:
        signals.append("Koers boven de bovenste Bollinger-band: sterk, maar uitgerekt")
    elif bb_low[-1] and price < bb_low[-1]:
        signals.append("Koers onder de onderste Bollinger-band: zwak, maar uitgerekt")
    if support or resistance:
        parts = []
        if support:
            touches = supports[0]["touches"] if supports else 1
            parts.append(f"steun {_fmt(support)}" + (f" ({touches}x getest)" if touches > 1 else ""))
        if resistance:
            touches = resistances[0]["touches"] if resistances else 1
            parts.append(f"weerstand {_fmt(resistance)}" + (f" ({touches}x getest)" if touches > 1 else ""))
        signals.append("Dichtstbijzijnde " + " en ".join(parts))
    ta = max(-1.0, min(1.0, votes))
    combined = 0.6 * (score - 50) / 50 + 0.4 * ta

    if combined >= 0.2:
        direction = "long"
    elif combined <= -0.2:
        direction = "short"
    else:
        direction = "neutral"
    conviction = round(min(100.0, abs(combined) * 140))

    # ------------------------------------------------------------ niveaus
    plan: dict = {"direction": direction, "conviction": conviction, "price": price, "ta_score": round(ta, 3),
                  "model_score": score, "combined": round(combined, 3), "horizon_days": horizon,
                  "support": support, "resistance": resistance, "atr": atr, "sigma": sigma, "rsi": r_now}
    if direction == "long":
        entry_high = price
        if r_now is not None and r_now > 70:
            pull = max(x for x in (support, s20, price - 1.5 * atr) if x is not None and x < price)
            entry_high = pull
            signals.append(f"Oververhit: wacht op een terugval naar ~{_fmt(pull)} voor instap")
        entry_low = entry_high - 0.6 * atr
        if support and support < entry_high:
            entry_low = max(entry_low, support)
        entry = (entry_low + entry_high) / 2
        anchor = support if support and support < entry else entry - 1.5 * atr
        stop = min(anchor - 0.5 * atr, entry - 1.5 * atr)
        stop = max(stop, entry - 3.5 * atr)
        floor1 = max(entry + atr, price + 0.5 * atr)
        target1 = resistance if resistance and resistance > floor1 else max(entry + 2 * atr, price + atr)
        proj = price * math.exp(max(reg["slope"], 0) * horizon)
        target2 = max(target1 + 1.5 * atr, entry + 3.5 * atr, proj)
        prob = prob_up
        label = "LONG"
        action = "Kopen (inzetten op stijging)"
    elif direction == "short":
        entry_low = price
        if r_now is not None and r_now < 30:
            bounce = min(x for x in (resistance, s20, price + 1.5 * atr) if x is not None and x > price)
            entry_low = bounce
            signals.append(f"Oversold: wacht op een opleving naar ~{_fmt(bounce)} voor instap")
        entry_high = entry_low + 0.6 * atr
        if resistance and resistance > entry_low:
            entry_high = min(entry_high, resistance)
        entry = (entry_low + entry_high) / 2
        anchor = resistance if resistance and resistance > entry else entry + 1.5 * atr
        stop = max(anchor + 0.5 * atr, entry + 1.5 * atr)
        stop = min(stop, entry + 3.5 * atr)
        ceil1 = min(entry - atr, price - 0.5 * atr)
        target1 = support if support and support < ceil1 else min(entry - 2 * atr, price - atr)
        proj = price * math.exp(min(reg["slope"], 0) * horizon)
        target2 = min(target1 - 1.5 * atr, entry - 3.5 * atr, proj)
        target2 = max(target2, price * 0.05)
        prob = 1 - prob_up
        label = "SHORT"
        action = "Short gaan (inzetten op daling, bv. via CFD of futures)"
    else:
        entry_low = entry_high = entry = stop = target1 = target2 = None
        prob = max(prob_up, 1 - prob_up)
        label = "AFWACHTEN"
        action = "Geen duidelijke richting: niet instappen"
    if entry is not None:
        # Minimaal risico/rendement 1,2: anders mikken op het volgende niveau of verder weg
        risk = abs(entry - stop)
        if direction == "long" and target1 - entry < 1.2 * risk:
            if resistance and resistance < entry + 1.2 * risk:
                signals.append(f"Eerste weerstand al op {_fmt(resistance)}: daar kan de koers haperen")
            nxt = [lv["price"] for lv in resistances if entry + 1.2 * risk <= lv["price"] <= entry + 3 * risk]
            target1 = nxt[0] if nxt else entry + 1.5 * risk
            target2 = max(target2, target1 + 1.5 * atr, entry + 2.5 * risk)
        elif direction == "short" and entry - target1 < 1.2 * risk:
            if support and support > entry - 1.2 * risk:
                signals.append(f"Eerste steun al op {_fmt(support)}: daar kan de daling haperen")
            nxt = [lv["price"] for lv in supports if entry - 3 * risk <= lv["price"] <= entry - 1.2 * risk]
            target1 = nxt[0] if nxt else entry - 1.5 * risk
            target2 = max(min(target2, target1 - 1.5 * atr, entry - 2.5 * risk), price * 0.05)
        plan["target1"], plan["target2"] = target1, target2
        plan["rr"] = round(abs(target1 - entry) / risk, 2) if risk else None
        plan["risk_pct"] = round(risk / entry * 100, 1)
        plan["reward_pct"] = round(abs(target1 - entry) / entry * 100, 1)
    else:
        plan["rr"] = plan["risk_pct"] = plan["reward_pct"] = None
    plan.update({"label": label, "action": action, "prob": round(prob, 3), "entry_low": entry_low,
                 "entry_high": entry_high, "entry": entry, "stop": stop, "target1": target1, "target2": target2,
                 "signals": signals})

    # ------------------------------------------------------------ verwachting (kegel ±1σ) en scenario
    drift = 0.5 * reg["slope"]
    f_dates = future_dates(dates[-1], horizon, weekdays_only=(market == "stocks"))
    cone_mid = [price * math.exp(drift * t) for t in range(1, horizon + 1)]
    cone_low = [price * math.exp(drift * t - sigma * math.sqrt(t)) for t in range(1, horizon + 1)]
    cone_high = [price * math.exp(drift * t + sigma * math.sqrt(t)) for t in range(1, horizon + 1)]
    scenario = []
    if direction != "neutral":
        half = horizon // 2
        scenario = [[0, price], [half, target1], [horizon, target2]]
    plan["expected"] = {"mid": cone_mid[-1], "low": cone_low[-1], "high": cone_high[-1]}

    hist = 120
    s = max(0, n - hist)
    ch = []
    for i in range(max(s, reg["start"]), n):
        x = i - reg["start"]
        c = reg["intercept"] + reg["slope"] * x
        ch.append([i - s, math.exp(c), math.exp(c + 2 * reg["sigma"]), math.exp(c - 2 * reg["sigma"])])
    chart = {
        "dates": dates[s:],
        "close": closes[s:],
        "sma20": sma20[s:],
        "sma50": sma50[s:],
        "bb_upper": bb_up[s:],
        "bb_lower": bb_low[s:],
        "rsi": rsi[s:],
        "channel": ch,                       # [index, midden, boven, onder]
        "levels": [{"price": lv["price"], "touches": lv["touches"],
                    "kind": "steun" if lv["price"] < price else "weerstand"}
                   for lv in levels if lv["touches"] >= 2 and abs(lv["price"] / price - 1) < 0.3][:8],
        "future_dates": f_dates,
        "cone_mid": cone_mid,
        "cone_low": cone_low,
        "cone_high": cone_high,
        "scenario": scenario,                # [stappen vooruit, koers]
    }
    return {"plan": plan, "chart": chart}
