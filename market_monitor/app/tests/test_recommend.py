import json
import math
import random
from datetime import date, timedelta

import pytest

from market_monitor.app import MarketMonitor
from market_monitor.config import load_config
from market_monitor.db import ago, iso, utcnow
from market_monitor.hass import HomeAssistant, in_quiet_hours, recommendation_line
from market_monitor.recommend import Recommender
from market_monitor.summary import OUTPUT_SCHEMA, Summarizer
from market_monitor.technical import (
    build_plan, future_dates, regression, rsi_series, sma_series, swing_levels,
)
from market_monitor.web import create_app


def series(drift, n=150, vol=0.012, start=100.0, seed=3, wave=0.008):
    rnd = random.Random(seed)
    p, out = start, []
    for i in range(n):
        p *= 1 + drift + rnd.gauss(0, vol) + wave * math.sin(i / 7)
        out.append(p)
    d0 = date(2026, 5, 1)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)], out


# --------------------------------------------------------------------------- indicatoren
def test_basic_indicators():
    assert sma_series([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]
    up = [float(i) for i in range(1, 40)]
    assert rsi_series(up)[-1] == 100.0
    down = up[::-1]
    assert rsi_series(down)[-1] == pytest.approx(0.0)
    reg = regression([100 * math.exp(0.01 * i) for i in range(60)])
    assert reg["slope"] == pytest.approx(0.01, rel=1e-6) and reg["r2"] == pytest.approx(1.0)


def test_swing_levels_find_repeated_tops():
    v = []
    for _ in range(6):  # zaagtand tussen 90 en 110
        v += [90 + k * 4 for k in range(6)] + [110 - k * 4 for k in range(6)]
    levels = swing_levels(v, window=3, lookback=200)
    tops = [lv for lv in levels if lv["price"] > 105]
    bottoms = [lv for lv in levels if lv["price"] < 95]
    assert tops and tops[0]["touches"] >= 4 and bottoms and bottoms[0]["touches"] >= 4


def test_future_dates_skip_weekends_for_stocks():
    fd = future_dates("2026-11-06", 3, weekdays_only=True)  # vrijdag
    assert fd == ["2026-11-09", "2026-11-10", "2026-11-11"]
    assert future_dates("2026-11-06", 2, weekdays_only=False) == ["2026-11-07", "2026-11-08"]


# --------------------------------------------------------------------------- plan
@pytest.mark.parametrize("drift,score,direction", [(0.004, 75, "long"), (-0.004, 25, "short")])
def test_plan_levels_are_consistent(drift, score, direction):
    d, c = series(drift)
    r = build_plan(d, c, score, 0.5 + (score - 50) / 100, "crypto")
    p = r["plan"]
    assert p["direction"] == direction and p["label"] == direction.upper()
    if direction == "long":
        assert p["stop"] < p["entry_low"] <= p["entry_high"] < p["target1"] < p["target2"]
        assert p["prob"] == pytest.approx(0.75)
    else:
        assert p["stop"] > p["entry_high"] >= p["entry_low"] > p["target1"] > p["target2"] > 0
        assert p["prob"] == pytest.approx(0.75)
    assert p["rr"] >= 1.2  # minimaal risico/rendement
    assert 0 < p["conviction"] <= 100 and p["signals"]
    ch = r["chart"]
    assert len(ch["dates"]) == len(ch["close"]) == len(ch["sma20"]) == len(ch["rsi"]) == 120
    assert len(ch["future_dates"]) == len(ch["cone_low"]) == len(ch["cone_high"]) == 20
    assert all(lo <= mid <= hi for lo, mid, hi in zip(ch["cone_low"], ch["cone_mid"], ch["cone_high"]))
    assert ch["scenario"][0] == [0, p["price"]] and ch["scenario"][-1] == [20, p["target2"]]


def test_neutral_plan_has_no_trade():
    d, c = series(0.0, vol=0.004, wave=0.0, seed=11)
    ta = build_plan(d, c, 50, 0.5, "stocks")["plan"]["ta_score"]
    score = 50 - (0.4 * ta / 0.6) * 50  # model en techniek heffen elkaar op -> geen richting
    r = build_plan(d, c, score, 0.5, "stocks")
    p = r["plan"]
    assert abs(p["combined"]) < 0.01
    assert p["direction"] == "neutral" and p["label"] == "AFWACHTEN"
    assert p["entry"] is None and p["stop"] is None and p["rr"] is None and r["chart"]["scenario"] == []


def test_overbought_long_waits_for_pullback():
    d, c = series(0.0, vol=0.002, wave=0.0, seed=5)
    c = c[:-10] + [c[-11] * (1 + 0.012 * k) for k in range(1, 11)]  # steile stijging aan het eind -> RSI hoog
    r = build_plan(d, c, 80, 0.7, "stocks")
    p = r["plan"]
    assert p["direction"] == "long" and p["rsi"] > 70
    assert p["entry_high"] < p["price"]
    assert any("terugval" in s for s in p["signals"])


def test_plan_needs_history():
    with pytest.raises(ValueError):
        build_plan(["2026-01-01"] * 10, [1.0] * 10)


# --------------------------------------------------------------------------- aanbevelingsmotor
def _seed(db, market, ticker, drift, score, seed=1):
    d, c = series(drift, seed=seed)
    db.executemany("INSERT INTO prices(market, ticker, date, close) VALUES (?,?,?,?)",
                   [(market, ticker, dd, cc) for dd, cc in zip(d, c)])
    db.execute("INSERT INTO scores_latest(market, ticker, computed_at, score, prob_up, prob_source, timing, reasons)"
               " VALUES (?,?,?,?,?,?,?,?)", (market, ticker, iso(utcnow()), score, 0.5 + (score - 50) / 100, "model",
                                             "Stijgende trend", json.dumps([f"reden {ticker}"])))
    return c


def test_recommender_picks_strongest_and_tracks_history(cfg, db):
    _seed(db, "crypto", "AAA", 0.004, 74, seed=1)
    _seed(db, "crypto", "BBB", -0.005, 22, seed=2)
    _seed(db, "crypto", "CCC", 0.0, 52, seed=3)
    rec = Recommender(cfg, db)
    res = rec.update("crypto")
    assert res["changed"] and res["event"] is None  # eerste aanbeveling: geen popup
    pick = res["pick"]
    assert pick["ticker"] in ("AAA", "BBB") and pick["plan"]["direction"] in ("long", "short")
    state = db.get_state("recommendation_crypto")
    assert state["ticker"] == pick["ticker"] and state["since"] == state["updated_at"]
    rec.update("crypto")  # zelfde aanbeveling: geen nieuwe geschiedenisregel
    assert db.query_one("SELECT COUNT(*) n FROM recommendations")["n"] == 1
    full = rec.current("crypto")
    assert full["chart"]["close"] and full["reasons"] == [f"reden {pick['ticker']}"]
    hist = rec.history("crypto")
    assert hist[0]["ticker"] == pick["ticker"] and hist[0]["status"] == "loopt"
    assert rec.update("stocks") is None  # nog geen data voor aandelen


def test_history_marks_target_hit(cfg, db):
    db.execute("INSERT INTO recommendations(market, ticker, direction, created_at, price, entry, stop, target1, target2)"
               " VALUES ('stocks','EXRO','long','2026-11-01T10:00:00Z',100,100,95,110,120)")
    db.executemany("INSERT INTO prices(market, ticker, date, close) VALUES ('stocks','EXRO',?,?)",
                   [("2026-11-02", 104.0), ("2026-11-03", 111.0), ("2026-11-04", 93.0)])
    h = Recommender(cfg, db).history("stocks")[0]
    assert h["status"] == "doel 1 gehaald" and h["return_pct"] == pytest.approx(-7.0)


# --------------------------------------------------------------------------- samenvatting + API
def test_summary_includes_recommendation(cfg, db):
    assert "recommendation_comment" in OUTPUT_SCHEMA["required"]
    _seed(db, "stocks", "EXRO", 0.004, 76)
    Recommender(cfg, db).update("stocks")
    s = Summarizer(cfg, db)
    data = s.gather("stocks", ago(hours=1), iso(utcnow()))
    text, _ = s.build_payload("stocks", data, ago(hours=1), iso(utcnow()))
    payload = json.loads(text.split("\n\n", 1)[1])
    assert payload["aanbeveling"]["ticker"] == "EXRO" and payload["aanbeveling"]["label"] == "LONG"
    c = s.extractive_summary("stocks", data)
    assert c["recommendation_comment"].startswith("LONG EXRO")


def test_recommendation_and_test_notify_api(tmp_path):
    m = MarketMonitor(load_config(str(tmp_path)))
    _seed(m.db, "crypto", "AAA", 0.004, 74)
    c = create_app(m).test_client()
    r = c.get("/api/recommendation?market=crypto").json  # wordt bij eerste opvraag berekend
    assert r["recommendation"]["ticker"] == "AAA" and r["history"]
    assert c.get("/api/recommendation?market=stocks").json["recommendation"] is None
    assert c.get("/api/overview?market=crypto").json["recommendation"]["ticker"] == "AAA"
    t = c.post("/api/notify/test").json
    assert t["ok"] is False and "Home Assistant" in t["message"]


# --------------------------------------------------------------------------- telefoonmeldingen
def test_quiet_hours():
    assert in_quiet_hours("23-7", 23) and in_quiet_hours("23-7", 3) and not in_quiet_hours("23-7", 7)
    assert in_quiet_hours("1-5", 2) and not in_quiet_hours("1-5", 6)
    assert not in_quiet_hours("", 3) and not in_quiet_hours("onzin", 3)


class _R:
    def __init__(self, status=200, js=None):
        self.status_code, self._js, self.text = status, js, ""

    def json(self):
        return self._js


def _ha(monkeypatch, cfg, db, services=None):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    ha = HomeAssistant(cfg, db)
    posts = []

    def get(url, headers=None, timeout=None):
        if url.endswith("/addons/self/info"):
            return _R(js={"data": {"slug": "abc123_market_monitor"}})
        return _R(js=services or [])

    monkeypatch.setattr(ha.session, "get", get)
    monkeypatch.setattr(ha.session, "post", lambda url, headers=None, data=None, timeout=None:
                        posts.append((url, json.loads(data))) or _R())
    return ha, posts


def test_auto_discovers_phones_and_sends_popup(monkeypatch, cfg, db):
    services = [{"domain": "light", "services": {"turn_on": {}}},
                {"domain": "notify", "services": {"notify": {}, "mobile_app_pixel_8": {}, "mobile_app_iphone": {},
                                                  "persistent_notification": {}}}]
    ha, posts = _ha(monkeypatch, cfg, db, services)
    assert ha.notify_targets() == ["mobile_app_iphone", "mobile_app_pixel_8"]
    res = ha.send_test()
    assert res["ok"] and res["targets"] == ["mobile_app_iphone", "mobile_app_pixel_8"]
    url, body = posts[0]
    assert url.endswith("/services/notify/mobile_app_iphone")
    d = body["data"]
    assert d["url"] == d["clickAction"] == "/hassio/ingress/abc123_market_monitor"
    assert d["importance"] == "high" and d["priority"] == "high" and d["ttl"] == 0
    assert d["actions"][0]["uri"] == "/hassio/ingress/abc123_market_monitor"


def test_explicit_services_and_no_phone_message(monkeypatch, cfg, db):
    cfg.notify_service = "notify.mobile_app_a, mobile_app_b"
    ha, _ = _ha(monkeypatch, cfg, db)
    assert ha.notify_targets() == ["mobile_app_a", "mobile_app_b"]
    cfg.notify_service = ""
    ha2, _ = _ha(monkeypatch, cfg, db, services=[{"domain": "notify", "services": ["notify"]}])
    res = ha2.send_test()
    assert not res["ok"] and "Home Assistant-app" in res["message"]


def test_hourly_message_has_recommendation_and_respects_quiet_hours(monkeypatch, cfg, db):
    cfg.notify_service = "mobile_app_x"
    _seed(db, "crypto", "AAA", 0.004, 74)
    Recommender(cfg, db).update("crypto")
    db.execute("INSERT INTO summaries(market, period_start, period_end, created_at, method, content) VALUES "
               "('crypto',?,?,?,?,?)", (ago(hours=1), iso(utcnow()), iso(utcnow()), "automatisch",
                                         json.dumps({"headline": "Bitcoin stijgt", "key_points": []})))
    ha, posts = _ha(monkeypatch, cfg, db)
    ha.publish_summaries(["crypto"])
    msg = next(b for u, b in posts if "/services/notify/" in u)["message"]
    assert msg.splitlines()[0] == "Crypto: Bitcoin stijgt" and msg.splitlines()[1].startswith("Aanbeveling: LONG AAA")
    ha.publish_recommendation("crypto")
    sensor = next(b for u, b in posts if u.endswith("sensor.market_monitor_crypto_recommendation"))
    assert sensor["state"] == "LONG AAA" and sensor["attributes"]["stop"] < sensor["attributes"]["target1"]

    posts.clear()
    cfg.quiet_hours = "0-24"
    monkeypatch.setattr("market_monitor.hass.local_hour", lambda: 12)
    cfg.quiet_hours = "8-20"
    ha.publish_summaries(["crypto"])
    assert not [u for u, _ in posts if "/services/notify/" in u]


def test_recommendation_line():
    assert recommendation_line(None) == ""
    assert recommendation_line({"direction": "neutral", "ticker": "X"}) == "Aanbeveling: afwachten (X)"
    line = recommendation_line({"direction": "short", "label": "SHORT", "ticker": "X", "entry_low": 10,
                                "entry_high": 10.5, "target1": 9, "stop": 11.2})
    assert line == "Aanbeveling: SHORT X - instap $10,00-$10,50, doel $9,00, stop $11,20"
