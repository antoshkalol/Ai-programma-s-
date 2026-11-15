"""Tests voor v0.7: uitgebreide techniek, terugkijken op aanbevelingen, mijn posities, budgetplan en bronnen."""

import math

import pytest

from market_monitor.app import MarketMonitor
from market_monitor.config import load_config
from market_monitor.db import Database, iso, utcnow
from market_monitor.planner import PLAN_STATE, BudgetPlanner
from market_monitor.portfolio import Holdings
from market_monitor.recommend import Recommender, buy_verdict, path_hits
from market_monitor.sources.btc_whales import classify, whale_transfers
from market_monitor.sources.sec_treasury import parse_table
from market_monitor.sources.x_posts import parse_x_rss
from market_monitor.technical import build_plan, crossed, fibonacci, rsi_divergence, stochastic, trend_structure
from market_monitor.web import create_app

from test_recommend import _seed, series


class FakeFx:
    def eurusd(self):
        return 1.0


class FakeHass:
    enabled = True

    def __init__(self):
        self.sent, self.states = [], []

    def notify(self, title, message, kind="summary", tag=""):
        self.sent.append((title, message, kind))

    def set_state(self, entity, state, attrs):
        self.states.append((entity, state, attrs))


def _live(db, market, ticker, price):
    db.execute(
        "INSERT INTO price_meta(market, ticker, last_price, fetched_at) VALUES (?,?,?,?) "
        "ON CONFLICT(market, ticker) DO UPDATE SET last_price=excluded.last_price, fetched_at=excluded.fetched_at",
        (market, ticker, price, iso(utcnow())),
    )


# --------------------------------------------------------------------------- techniek
def test_extended_indicators():
    up = [100 * 1.01 ** i for i in range(80)]
    k, d = stochastic(up)
    assert k[-1] == pytest.approx(100) and all(x is None or 0 <= x <= 100 for x in k)
    assert crossed([1, 1, 3], [2, 2, 2], 3) == "op" and crossed([3, 3, 1], [2, 2, 2], 3) == "neer"
    assert crossed([3, 3, 3], [2, 2, 2], 3) is None
    wave = [100 + 10 * math.sin(i / 5) + i * 0.5 for i in range(120)]
    assert trend_structure(wave) == "stijgend"
    assert trend_structure([200 - x for x in wave]) == "dalend"
    fib = fibonacci(up)
    assert fib["direction"] == "op" and fib["high"] > fib["levels"][0]["price"] > fib["levels"][-1]["price"] > fib["low"]
    # positieve divergentie: lager dieptepunt in de koers, hoger dieptepunt in de RSI
    v = [100] * 20 + [90, 85, 80, 85, 90, 95, 92, 88, 84, 79, 83, 88, 92, 95, 97]
    rsi = [None] * len(v)
    rsi[22], rsi[29] = 25, 35
    div = rsi_divergence(v, rsi)
    assert div and div["kind"] == "positief"


def test_build_plan_has_scorecard_and_sma200():
    dates, closes = series(0.003, n=260, seed=5)
    res = build_plan(dates, closes, 70, 0.6, "stocks")
    ind = {i["name"]: i for i in res["plan"]["indicators"]}
    assert "200-daags gemiddelde" in ind and ind["200-daags gemiddelde"]["value"] != "-"
    assert {"MACD", "RSI (14)", "EMA 9 / EMA 21", "Stochastic (14,3)", "Trendstructuur", "RSI-divergentie"} <= set(ind)
    assert any(i["core"] for i in ind.values()) and any(not i["core"] for i in ind.values())
    assert all(i["signal"] in ("positief", "negatief", "neutraal", "let op") for i in ind.values())
    ch = res["chart"]
    assert len(ch["sma200"]) == len(ch["close"]) and len(ch["macd_hist"]) == len(ch["close"]) and ch["fib"]
    short = build_plan(*series(0.003, n=120, seed=5), 70, 0.6, "stocks")
    assert short["chart"]["sma200"] == []
    assert next(i for i in short["plan"]["indicators"] if i["name"] == "200-daags gemiddelde")["value"] == "-"


# --------------------------------------------------------------------------- koop-nu-oordeel
OLD = {"direction": "long", "label": "LONG", "entry_low": 98, "entry_high": 100, "stop": 94, "target1": 108,
       "target2": 115}
FRESH_LONG = {"plan": {"direction": "long", "label": "LONG", "conviction": 60, "combined": 0.4, "atr": 2,
                       "entry_low": 98, "entry_high": 100, "stop": 94, "target1": 108, "target2": 115}}


@pytest.mark.parametrize("price,code,buy", [
    (99, "valid_now", True), (101, "wait_pullback", False), (104, "too_far", False), (96, "valid_cheaper", True)])
def test_buy_verdict_by_price(price, code, buy):
    v = buy_verdict(OLD, price, {}, FRESH_LONG, age_days=2)
    assert v["code"] == code and v["buy_now"] is buy and v["fresh"].startswith("Huidige analyse: LONG")


def test_buy_verdict_events():
    path = [("2026-11-01", 99), ("2026-11-02", 109), ("2026-11-03", 93)]
    hits = path_hits("long", OLD, path)
    assert hits == {"stop": "2026-11-03", "target1": "2026-11-02", "target2": None, "first": "target1"}
    assert buy_verdict(OLD, 99, hits, FRESH_LONG, 3)["code"] == "late"
    stop_first = path_hits("long", OLD, [("2026-11-01", 93), ("2026-11-02", 110)])
    assert buy_verdict(OLD, 99, stop_first, FRESH_LONG, 3)["code"] == "stopped"
    assert buy_verdict(OLD, 99, {"target2": "2026-11-04", "target1": "x", "first": "target1"}, FRESH_LONG, 3)["code"] == "done"
    flipped = {"plan": {**FRESH_LONG["plan"], "direction": "short", "label": "SHORT"}}
    assert buy_verdict(OLD, 99, {}, flipped, 3)["code"] == "flipped"
    assert buy_verdict(OLD, 99, {}, FRESH_LONG, 45)["code"] == "expired"
    assert buy_verdict({"direction": "neutral"}, 99, {}, None, 1)["code"] == "neutral_then"
    short = {"direction": "short", "label": "SHORT", "entry_low": 100, "entry_high": 102, "stop": 106,
             "target1": 92, "target2": 85}
    assert buy_verdict(short, 101, {}, None, 1)["code"] == "valid_now"
    assert buy_verdict(short, 99.5, {}, None, 1)["code"] in ("wait_pullback", "too_far")
    assert buy_verdict(OLD, None, {}, None, 1)["buy_now"] is None


def test_review_endpoint_flow(cfg, db):
    _seed(db, "crypto", "AAA", 0.004, 76, seed=1)
    rec = Recommender(cfg, db)
    st = rec.update("crypto")["state"]
    hid = rec.history("crypto")[0]["id"]
    r = rec.review(hid)
    assert r["ticker"] == "AAA" and r["then"]["stop"] == st["stop"] and r["verdict"]["code"]
    assert r["chart"]["plan"]["stop"] == st["stop"] and r["fresh"]["indicators"]
    _live(db, "crypto", "AAA", st["stop"] * 0.97)
    r2 = rec.review(hid)
    assert r2["verdict"]["buy_now"] is False and r2["now"]["followed_pct"] < 0
    assert rec.review(9999) is None
    cur = rec.current("crypto")
    assert rec.verdict_current("crypto", cur)["code"] in ("stopped", "flipped")


# --------------------------------------------------------------------------- mijn posities
class StubRec:
    def __init__(self, plan=None):
        self.plan = plan

    def plan_for(self, market, ticker, score_row=None):
        return {"plan": self.plan, "name": ticker} if self.plan else None

    def candidates(self, market, limit=12):
        return []


def test_holding_lifecycle_and_notifications(cfg, db):
    hass = FakeHass()
    h = Holdings(cfg, db, StubRec(), hass, FakeFx())
    _live(db, "crypto", "AAA", 100)
    pos = h.add("crypto", "aaa", 500, None, levels={"stop": 90, "target1": 110, "target2": 120, "atr": 3})
    assert pos["ticker"] == "AAA" and pos["qty"] == pytest.approx(5) and pos["entry_price"] == 100
    assert h.evaluate(pos)["code"] == "hold" and h.run() == 0

    _live(db, "crypto", "AAA", 111)                 # doel 1: deels verkopen + melding
    assert h.run() == 1 and h.run() == 0           # maar één melding
    p = h.get(pos["id"])
    assert p["t1_done"] == 1 and p["trail_stop"] >= 100 * 1.005
    assert hass.sent[-1][2] == "alert" and "DEELS VERKOPEN" in hass.sent[-1][0]

    _live(db, "crypto", "AAA", 116)                 # hoogste koers stijgt: trailing stop schuift mee
    h.run()
    assert h.get(pos["id"])["trail_stop"] == pytest.approx(116 - 2 * 3)
    _live(db, "crypto", "AAA", 109)                 # onder de trailing stop: verkopen
    adv = h.evaluate(h.get(pos["id"]))
    assert adv["code"] == "sell" and "trailing" in adv["title"]
    assert h.run() == 1 and "trailing" in hass.sent[-1][0]

    closed = h.close(pos["id"], 109)
    assert closed["status"] == "closed" and h.evaluate(closed)["code"] == "closed"
    assert h.list() == [] and len(h.list("closed")) == 1


def test_holding_target2_with_and_without_momentum(cfg, db):
    strong = {"direction": "long", "conviction": 70, "rsi": 60, "label": "LONG"}
    h = Holdings(cfg, db, StubRec(strong), FakeHass(), FakeFx())
    _live(db, "stocks", "BBB", 50)
    pos = h.add("stocks", "BBB", 1000, 50, levels={"stop": 45, "target1": 55, "target2": 60, "atr": 1})
    _live(db, "stocks", "BBB", 61)
    adv = h.evaluate(h.get(pos["id"]))
    assert adv["code"] == "run" and adv["trail_stop"] == pytest.approx(61 - 1.5)
    weak = Holdings(cfg, db, StubRec({"direction": "neutral", "conviction": 10, "rsi": 55}), FakeHass(), FakeFx())
    assert weak.evaluate(weak.get(pos["id"]))["code"] == "sell"
    _live(db, "stocks", "BBB", 44)
    assert weak.evaluate(weak.get(pos["id"]))["title"] == "VERKOPEN: stop-loss geraakt"
    flip = Holdings(cfg, db, StubRec({"direction": "short", "conviction": 55, "label": "SHORT"}), FakeHass(), FakeFx())
    _live(db, "stocks", "BBB", 52)
    assert flip.evaluate(flip.get(pos["id"]))["code"] == "warn"


def test_holding_validation_defaults_and_update(cfg, db):
    h = Holdings(cfg, db, StubRec(), None, FakeFx())
    with pytest.raises(ValueError, match="bedrag"):
        h.add("crypto", "AAA", 0, 10)
    with pytest.raises(ValueError, match="Geen koers"):
        h.add("crypto", "ZZZ", 100)
    pos = h.add("stocks", "CCC", 200, 20)            # geen analyse: niveaus uit de gemiddelde dagbeweging
    assert pos["stop"] < 20 < pos["target1"] < pos["target2"]
    upd = h.update(pos["id"], {"amount_eur": "400", "stop": "18,5"})
    assert upd["stop"] == 18.5 and upd["qty"] == pytest.approx(20)
    assert h.tickers("stocks") == ["CCC"] and h.delete(pos["id"])


# --------------------------------------------------------------------------- budgetplan
def _cand(ticker, market, price, lo, hi, stop, t1, t2, conv=70, prob=0.62):
    return {"market": market, "ticker": ticker, "name": ticker, "reasons": ["reden"],
            "plan": {"direction": "long", "price": price, "entry_low": lo, "entry_high": hi, "stop": stop,
                     "target1": t1, "target2": t2, "conviction": conv, "prob": prob, "atr": 2, "signals": ["sig"],
                     "horizon_days": 20}}


class StubCands(StubRec):
    def __init__(self, cands):
        super().__init__({"direction": "long"})
        self.cands = cands

    def candidates(self, market, limit=12):
        return [c for c in self.cands if c["market"] == market]


def test_budget_plan_allocation(cfg, db):
    cands = [_cand("AAA", "stocks", 100, 98, 101, 95, 110, 118),
             _cand("BBB", "stocks", 105, 98, 101, 95, 112, 120),            # boven de zone: limietorder
             _cand("CCC", "crypto", 50, 49, 51, 46, 58, 64),
             _cand("DDD", "crypto", 20, 19.5, 20.5, 19, 20.6, 21, conv=20)]  # te zwak
    pl = BudgetPlanner(cfg, db, StubCands(cands), fx=FakeFx())
    with pytest.raises(ValueError):
        pl.save(10, "normaal", "beide")
    pl.save("2.000,00", "normaal", "beide")
    plan = pl.build()
    items = {i["ticker"]: i for i in plan["items"]}
    assert set(items) == {"AAA", "BBB", "CCC"} and plan["rejected"][0]["ticker"] == "DDD"
    assert items["AAA"]["when"] == "nu" and items["BBB"]["when"] == "limiet" and "limietorder" in items["BBB"]["timing"]
    for i in items.values():
        assert i["amount_eur"] <= 2000 * 0.30 + 0.01                       # max 30% per positie
        assert i["max_loss_eur"] <= 2000 * 0.015 + 0.01                    # max 1,5% risico
    t = plan["totals"]
    assert t["cash"] >= 2000 * 0.15 - 0.01 and t["invested"] + t["cash"] == pytest.approx(2000)
    assert t["max_loss"] > 0 and t["profit_t2"] > 0 and plan["analysis"].startswith("Met een budget van €2.000")
    careful = pl.build(profile="voorzichtig")
    assert careful["totals"]["invested"] <= 2000 * 0.70 + 0.01
    assert pl.build(markets="crypto")["items"][0]["ticker"] == "CCC"


def test_budget_entry_alert_once(cfg, db):
    hass = FakeHass()
    cands = [_cand("BBB", "stocks", 105, 98, 101, 95, 112, 120)]
    pl = BudgetPlanner(cfg, db, StubCands(cands), hass=hass, fx=FakeFx())
    pl.save(1000, "offensief", "stocks")
    pl.build()
    _live(db, "stocks", "BBB", 104)
    assert pl.check_entries() == 0
    _live(db, "stocks", "BBB", 100)
    assert pl.check_entries() == 1 and "Instapmoment" in hass.sent[-1][0]
    pl.build()                                                     # opnieuw berekenen: geen dubbele melding
    assert pl.check_entries() == 0 and db.get_state(PLAN_STATE)["notified"]


# --------------------------------------------------------------------------- bronnen
def test_whale_classification():
    assert classify(25, 1) == "consolidatie" and classify(1, 40) == "bulk-uitbetaling"
    assert classify(1, 2) == "verplaatsing" and classify(5, 5) == "transfer"
    block = {"time": 1700000000, "tx": [{"hash": "h1", "inputs": [{"prev_out": {"addr": f"a{i}", "value": 10**8}}
                                                                 for i in range(20)],
                                         "out": [{"addr": "big", "value": 600 * 10**8}]}]}
    assert whale_transfers(block, 100)[0]["kind"] == "consolidatie"


def test_treasury_table_and_x_rss():
    txt = ("BTC Acquired Aggregate Purchase Price (in millions) Average Purchase Price Aggregate BTC Holdings "
           "November 3, 2025 - November 9, 2025 487 $ 49.9 $ 102,557 641,692")
    assert parse_table(txt, "BTC") == [{"side": "buy", "qty": 487.0, "value": 49.9e6}]
    assert parse_table("ETH Acquired 10", "ETH") == []
    rss = """<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
      <item><title>Rates will stay higher for longer</title><link>https://nitter.example/federalreserve/status/123#m</link>
        <pubDate>Fri, 14 Nov 2026 10:00:00 GMT</pubDate></item>
      <item><title>RT by @federalreserve: something</title><link>https://nitter.example/x/status/5</link></item>
    </channel></rss>"""
    items = parse_x_rss(rss, "federalreserve")
    assert len(items) == 1 and items[0]["id"] == "x:123" and items[0]["kind"] == "post"
    assert items[0]["url"] == "https://x.com/federalreserve/status/123" and items[0]["published_at"].startswith("2026-11-14")


def test_rebuild_keeps_user_data(cfg):
    db = Database(cfg.db_path)
    db.execute("INSERT INTO user_holdings(market, ticker, qty, entry_price, amount_eur, bought_at, created_at)"
               " VALUES ('crypto','BTC',1,100,100,'2026-01-01','2026-01-01')")
    db.set_state("user_budget", {"amount": 1234})
    db.conn.execute("PRAGMA user_version=1")
    db.conn.commit()
    db2 = Database(cfg.db_path)
    assert db2.query_one("SELECT COUNT(*) n FROM user_holdings")["n"] == 1
    assert db2.get_state("user_budget")["amount"] == 1234


# --------------------------------------------------------------------------- web
def test_advice_api(tmp_path):
    cfg = load_config(str(tmp_path))
    m = MarketMonitor(cfg)
    m.holdings.fx = m.planner.fx = FakeFx()
    _seed(m.db, "crypto", "AAA", 0.004, 76, seed=1)
    c = create_app(m).test_client()
    r = c.get("/api/recommendation?market=crypto").json
    assert r["verdict"]["title"] and r["history"][0]["id"]
    rv = c.get(f"/api/recommendation/review/{r['history'][0]['id']}").json
    assert rv["ticker"] == "AAA" and rv["chart"]
    assert c.get("/api/recommendation/review/999").status_code == 404
    bad = c.post("/api/holdings", json={"market": "crypto", "ticker": "AAA"})
    assert bad.status_code == 400
    price = r["recommendation"]["plan"]["price"]
    ok = c.post("/api/holdings", json={"market": "crypto", "ticker": "AAA", "amount_eur": "250", "price": price,
                                       "stop": price * 0.9, "target1": price * 1.1, "target2": price * 1.2}).json
    hid = ok["holding"]["id"]
    lst = c.get("/api/holdings").json
    assert lst["totals"]["count"] == 1 and lst["open"][0]["live"]["code"] in ("hold", "warn", "run", "partial")
    assert c.post(f"/api/holdings/{hid}", json={"note": "test"}).json["holding"]["note"] == "test"
    assert c.post(f"/api/holdings/{hid}/close", json={}).json["holding"]["status"] == "closed"
    assert c.post(f"/api/holdings/{hid}/delete").json["ok"]
    assert c.get("/api/budget").json["plan"] is None
    assert c.post("/api/budget", json={"amount": 10}).status_code == 400
    b = c.post("/api/budget", json={"amount": 3000, "profile": "offensief", "markets": "beide"}).json
    assert b["ok"] and b["plan"]["ok"] and "totals" in b["plan"]
    assert c.post("/api/budget/ai").status_code == 400                       # geen AI-sleutel of geen posities
    assert c.get("/api/tickers?market=crypto").json[0]["ticker"] == "AAA"
    soc = c.get("/api/social?market=stocks").json
    assert soc["token"] is False and "x_bearer_token" in soc["message"]
    assert c.get("/api/news?market=stocks&kind=post").json == []
