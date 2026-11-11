import json
from datetime import datetime, timedelta, timezone

import pytest

from market_monitor.app import MarketMonitor
from market_monitor.config import load_config
from market_monitor.db import ago, iso, utcnow
from market_monitor.hass import HomeAssistant
from market_monitor.recommend import Recommender, plan_status
from market_monitor.sources import prices
from market_monitor.web import create_app

from test_recommend import _seed


def _live(db, market, ticker, price):
    db.execute(
        "INSERT INTO price_meta(market, ticker, last_price, fetched_at) VALUES (?,?,?,?) "
        "ON CONFLICT(market, ticker) DO UPDATE SET last_price=excluded.last_price, fetched_at=excluded.fetched_at",
        (market, ticker, price, iso(utcnow())),
    )


def _score(db, market, ticker, score):
    db.execute("UPDATE scores_latest SET score=?, prob_up=? WHERE market=? AND ticker=?",
               (score, 0.5 + (score - 50) / 100, market, ticker))


def _age(db, market, hours):
    st = db.get_state(f"recommendation_{market}")
    st["since"] = ago(hours=hours)
    db.set_state(f"recommendation_{market}", st)
    return st


@pytest.fixture
def long_setup(cfg, db):
    _seed(db, "crypto", "AAA", 0.004, 76, seed=1)
    rec = Recommender(cfg, db)
    res = rec.update("crypto")
    st = res["state"]
    assert st["ticker"] == "AAA" and st["direction"] == "long"
    return rec, st


# --------------------------------------------------------------------------- status
def test_plan_status_long_and_short():
    lng = {"direction": "long", "entry_low": 98, "entry_high": 100, "stop": 94, "target1": 108, "target2": 115}
    assert plan_status(lng, 99)["code"] == "in_zone"
    assert plan_status(lng, 103)["code"] == "above_zone"
    assert plan_status(lng, 96)["code"] == "below_zone"
    assert plan_status(lng, 94)["code"] == "stop"
    assert plan_status(lng, 109)["code"] == "target1"
    assert plan_status(lng, 116)["code"] == "target2"
    sht = {"direction": "short", "entry_low": 100, "entry_high": 102, "stop": 106, "target1": 92, "target2": 85}
    assert plan_status(sht, 101)["code"] == "in_zone"
    assert plan_status(sht, 97)["code"] == "below_zone" and "niet najagen" in plan_status(sht, 97)["text"]
    assert plan_status(sht, 107)["code"] == "stop"
    assert plan_status(sht, 90)["code"] == "target1"
    assert plan_status({"direction": "neutral"}, 5)["code"] == "neutral"


# --------------------------------------------------------------------------- evaluatie
def test_small_move_keeps_plan_and_updates_live_price(long_setup, db):
    rec, st = long_setup
    _live(db, "crypto", "AAA", (st["entry_low"] + st["entry_high"]) / 2)
    res = rec.update("crypto")
    assert not res["changed"] and res["event"] is None
    now = db.get_state("recommendation_crypto")
    assert now["stop"] == st["stop"] and now["target1"] == st["target1"]  # vaste niveaus
    assert now["status"]["code"] == "in_zone" and now["live_price"] != st["price"]
    assert db.query_one("SELECT COUNT(*) n FROM recommendations")["n"] == 1


def test_stop_hit_closes_plan_and_blocks_same_side(long_setup, db):
    rec, st = long_setup
    _seed(db, "crypto", "BBB", -0.005, 22, seed=2)
    _live(db, "crypto", "AAA", st["stop"] * 0.99)
    res = rec.update("crypto")
    assert res["changed"] and res["event"] == "stop"
    assert res["previous"]["ticker"] == "AAA" and "Stop-loss" in res["reason"]
    closed = db.query_one("SELECT * FROM recommendations WHERE ticker='AAA'")
    assert closed["outcome"] == "stop geraakt" and closed["close_price"] == pytest.approx(st["stop"] * 0.99)
    assert not (res["state"]["ticker"] == "AAA" and res["state"]["direction"] == "long")  # afkoeling
    hist = rec.history("crypto")
    assert any(h["ticker"] == "AAA" and h["status"] == "stop geraakt" and h["return_pct"] < 0 for h in hist)


def test_target1_moves_stop_to_entry_once(long_setup, db):
    rec, st = long_setup
    _live(db, "crypto", "AAA", (st["target1"] + st["target2"]) / 2)
    res = rec.update("crypto")
    assert res["event"] == "target1" and not res["changed"]
    now = db.get_state("recommendation_crypto")
    assert now["target1_hit"] and now["stop"] == pytest.approx(st["entry"])
    assert rec.update("crypto")["event"] is None  # geen tweede melding


def test_direction_flip_issues_new_plan(long_setup, db):
    rec, st = long_setup
    _score(db, "crypto", "AAA", 3)  # koopscore stort in -> techniek + model wijzen naar short
    _live(db, "crypto", "AAA", (st["entry_low"] + st["entry_high"]) / 2)
    res = rec.update("crypto")
    assert res["changed"] and res["event"] == "direction"
    assert res["state"]["direction"] != "long" and "gewijzigd van LONG" in res["reason"]


def test_switch_needs_clearly_stronger_candidate_and_hold_time(long_setup, db):
    rec, st = long_setup
    _score(db, "crypto", "AAA", 56)
    _seed(db, "crypto", "BBB", -0.006, 12, seed=2)
    _live(db, "crypto", "AAA", (st["entry_low"] + st["entry_high"]) / 2)
    assert rec.update("crypto")["event"] is None  # nog geen uur oud: niet wisselen
    _age(db, "crypto", 2)
    res = rec.update("crypto")
    assert res["event"] == "switch" and res["state"]["ticker"] == "BBB" and res["previous"]["ticker"] == "AAA"


def test_refresh_after_24h_outside_zone(long_setup, db):
    rec, st = long_setup
    price = st["entry_high"] * 1.004
    assert price < st["target1"]
    _live(db, "crypto", "AAA", price)
    assert rec.update("crypto")["event"] is None
    aged = _age(db, "crypto", 25)
    res = rec.update("crypto")
    assert res["event"] == "refresh" and res["state"]["ticker"] == "AAA"
    new = res["state"]
    assert new["entry_high"] != st["entry_high"] and new["stop"] != st["stop"]  # niveaus opnieuw berekend
    assert new["since"] > aged["since"] and new["issued_price"] == pytest.approx(price)
    old = db.query_one("SELECT outcome FROM recommendations WHERE id=1")
    assert old["outcome"] == "plan bijgewerkt"


def test_neutral_turns_into_new_opportunity(cfg, db):
    _seed(db, "crypto", "AAA", 0.004, 76, seed=1)
    db.set_state("recommendation_crypto", {"ticker": "ZZZ", "direction": "neutral", "label": "AFWACHTEN",
                                           "entry_low": None, "entry_high": None, "since": ago(minutes=5)})
    res = Recommender(cfg, db).update("crypto")
    assert res["event"] == "new" and res["state"]["ticker"] == "AAA"


def test_current_uses_fixed_levels_and_live_status(long_setup, db):
    rec, st = long_setup
    _live(db, "crypto", "AAA", st["entry_high"] * 1.01)
    cur = rec.current("crypto")
    assert cur["plan"]["stop"] == st["stop"] and cur["plan"]["target1"] == st["target1"]
    assert cur["status"]["code"] == "above_zone"
    assert cur["chart"]["scenario"][1] == [10, st["target1"]]


# --------------------------------------------------------------------------- meldingen
def test_change_notifications_are_throttled_except_stop(monkeypatch, cfg, db):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    cfg.notify_service = "mobile_app_x"
    ha = HomeAssistant(cfg, db)
    sent = []
    monkeypatch.setattr(ha, "call_service", lambda d, s, data: sent.append(data) or True)
    monkeypatch.setattr(ha, "panel_url", lambda: "/")
    st = {"ticker": "BBB", "label": "SHORT", "direction": "short", "entry_low": 10, "entry_high": 10.5,
          "target1": 9, "target2": 8, "stop": 11, "live_price": 10.2}
    ha.notify_recommendation("crypto", {"event": "switch", "state": st, "reason": "BBB is sterker"})
    ha.notify_recommendation("crypto", {"event": "switch", "state": st, "reason": "nog eens"})
    assert len(sent) == 1
    assert sent[0]["title"] == "Aanbeveling crypto: SHORT BBB"
    assert sent[0]["message"].startswith("BBB is sterker.\nNu: SHORT BBB - instap $10,00-$10,50")
    assert sent[0]["data"]["tag"] == "market_monitor_reco_crypto"
    ha.notify_recommendation("crypto", {"event": "stop", "state": st, "reason": "Stop-loss van LONG AAA geraakt"})
    ha.notify_recommendation("crypto", {"event": "target1", "state": st, "reason": "x"})
    assert len(sent) == 3 and sent[2]["title"] == "Doel 1 gehaald: SHORT BBB"
    cfg.notify_recommendation_changes = False
    ha.notify_recommendation("crypto", {"event": "stop", "state": st, "reason": "x"})
    assert len(sent) == 3


def test_recommend_job_publishes_and_notifies(monkeypatch, tmp_path):
    m = MarketMonitor(load_config(str(tmp_path)))
    _seed(m.db, "crypto", "AAA", 0.004, 76, seed=1)
    calls = []
    monkeypatch.setattr(m.hass, "publish_recommendation", lambda market: calls.append(("pub", market)))
    monkeypatch.setattr(m.hass, "notify_recommendation", lambda market, res: calls.append(("notify", res["event"])))
    assert m._recommend_job() == 1 and calls == [("pub", "crypto")]  # eerste keer: geen popup
    st = m.db.get_state("recommendation_crypto")
    _live(m.db, "crypto", "AAA", st["stop"] * 0.98)
    m._recommend_job()
    assert ("notify", "stop") in calls


def test_ai_comment_only_for_current_plan(tmp_path):
    m = MarketMonitor(load_config(str(tmp_path)))
    _seed(m.db, "crypto", "AAA", 0.004, 76, seed=1)
    st = m.recommender.update("crypto")["state"]
    c = create_app(m).test_client()

    def put_summary(ref):
        m.db.execute("INSERT INTO summaries(market, period_start, period_end, created_at, method, content) VALUES"
                     " ('crypto',?,?,?,?,?)", (ago(hours=1), iso(utcnow()), iso(utcnow()), "ai",
                                               json.dumps({"headline": "h", "recommendation_comment": "Goed plan",
                                                           "recommendation_ref": ref})))
    put_summary({"ticker": "AAA", "direction": "long", "since": st["since"]})
    assert c.get("/api/recommendation?market=crypto").json["ai_comment"] == "Goed plan"
    put_summary({"ticker": "OLD", "direction": "short", "since": "2026-01-01T00:00:00Z"})
    r = c.get("/api/recommendation?market=crypto").json
    assert r["ai_comment"] is None and r["recommendation"]["status"]["code"]


# --------------------------------------------------------------------------- koersen ophalen
UTC = timezone.utc


def test_market_hours_and_due_logic():
    tue_open = datetime(2026, 11, 10, 16, 0, tzinfo=UTC)      # 11:00 in New York
    sat = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
    assert prices.us_market_open(tue_open) and not prices.us_market_open(sat)
    assert prices.last_close(sat) == datetime(2026, 11, 13, 21, 15, tzinfo=UTC)  # vrijdag 16:15 New York
    due = prices.PriceSource.due
    assert due(None, False, tue_open)
    assert due(tue_open - timedelta(minutes=15), True, tue_open)
    assert not due(tue_open - timedelta(minutes=15), False, tue_open)
    assert due(tue_open - timedelta(minutes=60), False, tue_open)
    assert due(datetime(2026, 11, 13, 20, 0, tzinfo=UTC), True, sat)       # voor het slot opgehaald
    assert not due(datetime(2026, 11, 13, 21, 30, tzinfo=UTC), True, sat)  # na het slot: klaar tot maandag


class _Resp:
    def __init__(self, status=200, js=None):
        self.status_code, self._js, self.text, self.ok = status, js, "", status < 400

    def json(self):
        return self._js


def test_twelve_data_budget_and_skipping_fresh_tickers(cfg, db):
    from test_prices_summary import NASDAQ_JSON, TWELVE_JSON
    cfg.stock_price_api_key = "k"
    calls = []

    class S:
        def request(self, method, url, timeout=None, **kw):
            calls.append(url)
            if "twelvedata" in url:
                return _Resp(js=TWELVE_JSON)
            if "nasdaq" in url:
                return _Resp(js=NASDAQ_JSON)
            return _Resp(429)

    db.set_state("td_budget", {"date": utcnow().strftime("%Y-%m-%d"), "used": prices.TD_DAILY_BUDGET})
    src = prices.PriceSource(cfg, db, S(), lambda: ["AAPL"], lambda: ["AAPL"])
    assert src.run() == 1
    assert not any("twelvedata" in u for u in calls)  # budget op: Twelve Data overgeslagen
    assert db.get_state("price_sources")["used"] == {"nasdaq": 1}
    calls.clear()
    db.set_state("price_fetched", {"AAPL": iso(utcnow())})
    if not prices.us_market_open(utcnow()):
        assert src.run() == 0 and calls == []  # net opgehaald en markt dicht: niets te doen
