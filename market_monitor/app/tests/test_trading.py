"""Tests voor de handelsmodule: instellingen, signalen, positiegrootte, paper-broker, risico, AI en brokers."""

import base64
import hashlib
import hmac
import json
from datetime import timedelta

import pytest

from market_monitor.app import MarketMonitor
from market_monitor.config import load_config
from market_monitor.db import Database, iso, utcnow
from market_monitor.recommend import Recommender
from market_monitor.trading import settings as ts
from market_monitor.trading.brokers import (BitvavoBroker, BrokerError, Order, PaperBroker, Trading212Broker,
                                            fmt_qty, fmt_sig)
from market_monitor.trading.engine import CONFIRM_TEXT, TradingEngine
from market_monitor.trading.signals import levels_for
from market_monitor.web import create_app

from test_recommend import _seed


class FakeFx:
    def __init__(self, rate=1.0):
        self.rate = rate

    def eurusd(self):
        return self.rate


class FakeHass:
    enabled = True

    def __init__(self):
        self.sent, self.states = [], []

    def notify(self, title, message, kind="summary", tag=""):
        self.sent.append((title, message, kind))

    def set_state(self, entity, state, attrs):
        self.states.append((entity, state, attrs))


def _live(db, market, ticker, price, minutes_ago=0):
    db.execute(
        "INSERT INTO price_meta(market, ticker, last_price, fetched_at) VALUES (?,?,?,?) "
        "ON CONFLICT(market, ticker) DO UPDATE SET last_price=excluded.last_price, fetched_at=excluded.fetched_at",
        (market, ticker, price, iso(utcnow() - timedelta(minutes=minutes_ago))),
    )


def _last_close(db, market, ticker):
    return db.query_one("SELECT close FROM prices WHERE market=? AND ticker=? ORDER BY date DESC LIMIT 1",
                        (market, ticker))["close"]


def _settings(**over):
    s = dict(ts.DEFAULTS)
    s.update({"budget_stocks_eur": 0.0, "budget_crypto_eur": 1000.0, "ai_enabled": False, "trade_style": "kort",
              "min_rr": 1.2, "min_conviction": 30, "notify_trades": "alle"})
    s.update(over)
    return s


def _engine(cfg, db, settings=None, **kw):
    hass = kw.pop("hass", FakeHass())
    eng = TradingEngine(cfg, db, Recommender(cfg, db), kw.pop("summarizer", None), hass, fx=FakeFx(kw.pop("fx", 1.0)),
                        **kw)
    db.set_state(ts.STATE_KEY, settings or _settings())
    return eng, hass


@pytest.fixture
def up(db):
    _seed(db, "crypto", "AAA", 0.004, 76, seed=1)
    price = _last_close(db, "crypto", "AAA")
    _live(db, "crypto", "AAA", price)
    return price


# --------------------------------------------------------------------------- instellingen
def test_settings_validate_bounds_and_types(db):
    out, errors = ts.validate({"risk_per_trade_pct": "2,5", "allowed_tickers": "aapl, $nvda ,", "ai_mode": "filter",
                               "allow_short": "nee"})
    assert not errors
    assert out["risk_per_trade_pct"] == 2.5 and out["allowed_tickers"] == ["AAPL", "NVDA"]
    assert out["ai_mode"] == "filter" and out["allow_short"] is False
    _, errors = ts.validate({"risk_per_trade_pct": 50, "ai_model": "gpt", "max_open_positions": "x"})
    assert set(errors) == {"risk_per_trade_pct", "ai_model", "max_open_positions"}
    _, errors = ts.validate({"weight_ta": 0, "weight_flow": 0, "weight_news": 0, "weight_x": 0})
    assert "weight_ta" in errors
    new, errors = ts.save(db, {"max_open_positions": 7})
    assert not errors and ts.load(db)["max_open_positions"] == 7
    schema = ts.schema()
    assert {f["key"] for f in schema["fields"]} == set(ts.DEFAULTS)
    assert all(f["group"] in {g["key"] for g in schema["groups"]} for f in schema["fields"])


def test_database_rebuild_keeps_trading_data(cfg):
    db = Database(cfg.db_path)
    ts.save(db, {"max_open_positions": 9})
    db.execute("INSERT INTO trading_runs(name, mode, status, created_at, settings, capital) VALUES "
               "('x','paper','finished','2026-01-01T00:00:00Z','{}','{}')")
    db.execute("INSERT INTO news(id, title, fetched_at) VALUES ('n1', 't', '2026-01-01')")
    db.conn.execute("PRAGMA user_version=1")
    db.conn.commit()
    db2 = Database(cfg.db_path)
    assert db2.query_one("SELECT COUNT(*) n FROM news")["n"] == 0            # cache opnieuw opgebouwd
    assert db2.query_one("SELECT COUNT(*) n FROM trading_runs")["n"] == 1    # handelsadministratie bewaard
    assert ts.load(db2)["max_open_positions"] == 9


# --------------------------------------------------------------------------- niveaus en broker
def test_levels_for_plan_and_atr_based():
    s = _settings(trade_style="swing", min_rr=1.5, stop_atr_mult=1.5)
    plan = {"direction": "long", "entry_low": 98, "entry_high": 100, "stop": 95, "target1": 108, "target2": 115,
            "atr": 2.0, "support": 96, "resistance": 108}
    lv = levels_for("long", 100, plan, s)
    assert lv["plan_based"] and lv["stop"] == 95 and lv["target1"] == 108 and lv["rr"] == pytest.approx(1.6)
    assert "skip" in levels_for("long", 101, plan, s)           # boven de instapzone: niet najagen
    sh = levels_for("short", 100, plan, s)                       # plan wijst long: ATR-niveaus
    assert not sh["plan_based"] and sh["stop"] == pytest.approx(103) and sh["target1"] < 100
    assert sh["rr"] >= 1.5
    k = levels_for("long", 100, plan, _settings(trade_style="kort", stop_atr_mult=1.5))
    assert k["stop"] == pytest.approx(100 - 1.5 * 0.8)          # 0,4x de dagbeweging


def test_paper_broker_fees_and_slippage():
    b = PaperBroker(_settings(slippage_pct=0.1, fee_pct_crypto=0.25))
    f = b.execute(Order("crypto", "AAA", "buy", ref_price=110.0, fx=1.1, eur=200))
    assert f["price"] == pytest.approx(110.11) and f["price_eur"] == pytest.approx(100.1)
    assert f["value_eur"] == pytest.approx(200) and f["fee_eur"] == pytest.approx(0.5)
    s = b.execute(Order("crypto", "AAA", "sell", ref_price=110.0, fx=1.1, qty=1))
    assert s["price"] == pytest.approx(109.89)
    assert fmt_sig(12345.678, 5) == "12346" and fmt_sig(0.000123456, 5) == "0.00012346"
    assert fmt_qty(0.123456789, 4) == "0.1234"


# --------------------------------------------------------------------------- paper-run
def test_paper_run_opens_takes_profit_and_stops(cfg, db, up):
    eng, hass = _engine(cfg, db)
    run = eng.start_run("paper", "test", hours=4)
    assert run["status"] == "running" and run["capital"] == {"stocks": 0.0, "crypto": 1000.0}
    eng.tick()
    pos = eng.positions(run["id"])
    assert len(pos) == 1 and pos[0]["ticker"] == "AAA" and pos[0]["side"] == "long"
    p = pos[0]
    value = p["qty"] * p["entry_price"]
    assert value == pytest.approx(250, rel=0.01)                  # begrensd op 25% van het budget
    assert p["entry_price"] == pytest.approx(up * 1.001)          # slippage
    cash = eng.get_run(run["id"])["state"]["cash"]["crypto"]
    assert cash == pytest.approx(1000 - value - value * 0.0025, rel=1e-6)
    assert p["stop"] < p["entry_price"] < p["target1"] < p["target2"]
    assert any("geopend" in t for t, _, _ in hass.sent)

    _live(db, "crypto", "AAA", p["target1"] * 1.001)              # doel 1: helft verkopen, stop naar instap
    eng.tick()
    p2 = eng.positions(run["id"])[0]
    assert p2["t1_done"] == 1 and p2["qty"] == pytest.approx(p["qty"] / 2)
    assert p2["stop"] >= p["entry_price"]
    _live(db, "crypto", "AAA", p2["stop"] * 0.9995)               # winst-stop
    eng.tick()
    assert eng.positions(run["id"]) == []
    closed = eng.positions(run["id"], status="closed")[0]
    assert closed["pnl_eur"] > 0 and "Winst-stop" in closed["reason_close"]
    st = eng.stats(run["id"])
    assert st["trades"] == 1 and st["wins"] == 1 and st["pnl_eur"] > 0 and st["fees_eur"] > 0
    d = eng.detail(run["id"])
    assert d["equity"] and d["closed"] and any(x["outcome"] == "uitgevoerd" for x in d["decisions"])


def test_paper_short_and_finish(cfg, db):
    _seed(db, "crypto", "BBB", -0.005, 22, seed=2)
    price = _last_close(db, "crypto", "BBB")
    _live(db, "crypto", "BBB", price)
    eng, hass = _engine(cfg, db)
    run = eng.start_run("paper", "short", hours=1)
    eng.tick()
    p = eng.positions(run["id"])[0]
    assert p["side"] == "short" and p["stop"] > p["entry_price"] > p["target1"]
    cash = eng.get_run(run["id"])["state"]["cash"]["crypto"]
    assert cash > 1000                                           # opbrengst van de short in kas
    eng.tick(now=utcnow() + timedelta(hours=2))                   # testduur voorbij
    r = eng.get_run(run["id"])
    assert r["status"] == "finished" and r["stats"]["trades"] == 1 and eng.positions(run["id"]) == []
    assert any(t.startswith("Paper-test klaar") for t, _, _ in hass.sent)


def test_short_disabled_is_logged(cfg, db):
    _seed(db, "crypto", "BBB", -0.005, 22, seed=2)
    _live(db, "crypto", "BBB", _last_close(db, "crypto", "BBB"))
    eng, _ = _engine(cfg, db, _settings(allow_short=False))
    run = eng.start_run("paper")
    eng.tick()
    assert eng.positions(run["id"]) == []
    rej = db.query("SELECT reason FROM trading_decisions WHERE run_id=? AND outcome='afgewezen'", (run["id"],))
    assert any("shorten staat uit" in r["reason"] for r in rej)


def test_stale_price_blocks_trading(cfg, db, up):
    _live(db, "crypto", "AAA", up, minutes_ago=90)
    eng, _ = _engine(cfg, db)
    run = eng.start_run("paper")
    eng.tick()
    assert eng.positions(run["id"]) == []


def test_drawdown_halts_and_closes(cfg, db, up):
    eng, hass = _engine(cfg, db, _settings(max_drawdown_pct=1.0, max_position_pct=100, risk_per_trade_pct=5))
    run = eng.start_run("paper")
    eng.tick()
    assert eng.positions(run["id"])
    _live(db, "crypto", "AAA", up * 0.9)
    eng.tick()
    r = eng.get_run(run["id"])
    assert r["status"] == "halted" and "Noodrem" in r["halt_reason"]
    assert eng.positions(run["id"]) == []
    assert any(k == "alert" for _, _, k in hass.sent)


def test_daily_loss_pauses_new_trades(cfg, db, up):
    eng, _ = _engine(cfg, db, _settings(max_daily_loss_pct=0.5, max_drawdown_pct=50, max_position_pct=100,
                                        risk_per_trade_pct=5))
    run = eng.start_run("paper")
    eng.tick()
    _live(db, "crypto", "AAA", up * 0.95)
    eng.tick()
    r = eng.get_run(run["id"])
    assert r["status"] == "running" and r["state"]["paused"]
    _live(db, "crypto", "AAA", up)
    eng.tick(now=utcnow() + timedelta(minutes=20))               # nieuw beslismoment, maar gepauzeerd
    assert eng.positions(run["id"]) == []


def test_sizing_uses_risk_and_caps(cfg, db, up):
    eng, _ = _engine(cfg, db, _settings(risk_per_trade_pct=0.5, max_position_pct=100))
    run = eng.start_run("paper")
    sig = eng.signals.evaluate("crypto", run["settings"])[0]
    port = eng.portfolio(run, 1.0, utcnow())
    v1, _ = eng.size(run, "crypto", sig, sig["levels"], sig["conviction"], 1.0, 1.0, port)
    v2, _ = eng.size(run, "crypto", sig, sig["levels"], sig["conviction"], 0.5, 1.0, port)
    assert v2 == pytest.approx(v1 / 2)
    expected_risk = 1000 * 0.005 * (0.5 + 0.5 * sig["conviction"] / 100)
    assert v1 / sig["price"] * sig["levels"]["risk"] == pytest.approx(expected_risk)
    small, why = eng.size(run, "crypto", sig, sig["levels"], sig["conviction"], 0.001, 1.0, port)
    assert small == 0 and "te klein" in why


# --------------------------------------------------------------------------- AI
class FakeSummarizer:
    def __init__(self, response=None, fail=False):
        self.response, self.fail, self.calls = response, fail, []

    def ask_json(self, system, text, schema, effort=None, max_tokens=16000, model=None, usage=None):
        self.calls.append({"text": text, "model": model, "effort": effort})
        if self.fail:
            raise RuntimeError("geen verbinding")
        if usage is not None:
            usage.update({"model": model, "input": 10000, "output": 1000, "cache_read": 0, "cache_write": 0})
        return self.response


def test_ai_approves_with_size_factor_and_tracks_cost(cfg, db, up):
    cfg.anthropic_api_key = "sk-test"
    ai = FakeSummarizer({"market_view": "Bitcoin sterk", "decisions": [
        {"ticker": "AAA", "action": "open_long", "size_factor": 0.5, "confidence": 0, "stop": 0,
         "reason": "Trend en nieuws positief"},
        {"ticker": "ZZZ", "action": "open_long", "size_factor": 1, "confidence": 90, "stop": 0, "reason": "gok"}]})
    eng, _ = _engine(cfg, db, _settings(ai_enabled=True, risk_per_trade_pct=0.2, max_position_pct=100),
                     summarizer=ai)
    run = eng.start_run("paper")
    sig = eng.signals.evaluate("crypto", run["settings"])[0]
    full, _ = eng.size(run, "crypto", sig, sig["levels"], sig["conviction"], 1.0, 1.0,
                       eng.portfolio(run, 1.0, utcnow()))
    eng.tick()
    assert ai.calls and ai.calls[0]["model"] == "claude-opus-5-5" and '"AAA"' in ai.calls[0]["text"]
    p = eng.positions(run["id"])[0]
    assert p["source"] == "ai" and "AI: Trend en nieuws positief" in p["reason_open"]
    assert p["qty"] * p["entry_price"] == pytest.approx(full / 2, rel=0.01)
    r = eng.get_run(run["id"])
    assert r["ai_calls"] == 1 and r["ai_cost_usd"] == pytest.approx((10000 * 4 + 1000 * 20) / 1e6)
    assert r["state"]["market_view"]["crypto"] == "Bitcoin sterk"
    rej = db.query("SELECT reason FROM trading_decisions WHERE run_id=? AND ticker='ZZZ'", (run["id"],))
    assert rej and "niet in de kandidatenlijst" in rej[0]["reason"]


def test_ai_skip_rejects_and_failure_falls_back_to_rules(cfg, db, up):
    cfg.anthropic_api_key = "sk-test"
    ai = FakeSummarizer({"market_view": "", "decisions": [
        {"ticker": "AAA", "action": "skip", "size_factor": 1, "confidence": 70, "stop": 0,
         "reason": "RSI oververhit, wacht op terugval"}]})
    eng, _ = _engine(cfg, db, _settings(ai_enabled=True), summarizer=ai)
    run = eng.start_run("paper")
    eng.tick()
    assert eng.positions(run["id"]) == []
    rej = db.query("SELECT actor, reason FROM trading_decisions WHERE run_id=? AND outcome='afgewezen'", (run["id"],))
    assert any(r["actor"] == "ai" and "oververhit" in r["reason"] for r in rej)

    ai2 = FakeSummarizer(fail=True)
    eng2, _ = _engine(cfg, db, _settings(ai_enabled=True), summarizer=ai2)
    run2 = eng2.start_run("paper")
    eng2.tick()
    assert eng2.positions(run2["id"]) and eng2.positions(run2["id"])[0]["source"] == "regels"
    errs = db.query("SELECT reason FROM trading_decisions WHERE run_id=? AND outcome='fout'", (run2["id"],))
    assert any("regels beslissen" in e["reason"] for e in errs)


# --------------------------------------------------------------------------- live
class FakeLive(PaperBroker):
    name = "Testbroker"
    live = True
    can_short = False

    def __init__(self, settings, uncertain=False):
        super().__init__(settings)
        self.uncertain = uncertain

    def execute(self, order):
        if self.uncertain:
            raise BrokerError("time-out", uncertain=True)
        return super().execute(order)

    def free_cash_eur(self, market):
        return 5000.0


def test_live_requires_switch_keys_and_confirmation(cfg, db, up):
    eng, _ = _engine(cfg, db, _settings(live_dry_run=False))
    with pytest.raises(ValueError, match="allow_live_trading"):
        eng.start_run("live")
    cfg.allow_live_trading = True
    with pytest.raises(ValueError, match="bitvavo_api_key"):
        eng.start_run("live")
    cfg.bitvavo_api_key, cfg.bitvavo_api_secret = "k", "s"
    with pytest.raises(ValueError, match="Typ ter bevestiging"):
        eng.start_run("live", confirm="ja")
    run = eng.start_run("live", confirm=CONFIRM_TEXT.lower())
    assert run["mode"] == "live" and run["settings"]["max_exposure_pct"] <= 100
    with pytest.raises(ValueError, match="al een live-run"):
        eng.start_run("live", confirm=CONFIRM_TEXT)


def test_live_cannot_short_and_uncertain_order_halts(cfg, db, up):
    cfg.allow_live_trading = True
    cfg.bitvavo_api_key, cfg.bitvavo_api_secret = "k", "s"
    _seed(db, "crypto", "BBB", -0.005, 22, seed=2)
    _live(db, "crypto", "BBB", _last_close(db, "crypto", "BBB"))
    eng, hass = _engine(cfg, db, broker_factory=lambda run, market: FakeLive(run["settings"]))
    run = eng.start_run("live")                                  # proefdraaien: geen bevestiging nodig
    eng.tick()
    sides = {p["ticker"]: p["side"] for p in eng.positions(run["id"])}
    assert sides == {"AAA": "long"}
    rej = db.query("SELECT reason FROM trading_decisions WHERE run_id=? AND ticker='BBB'", (run["id"],))
    assert any("short kan niet via de API" in r["reason"] for r in rej)
    eng.stop_run(run["id"])

    eng2, hass2 = _engine(cfg, db, broker_factory=lambda run, market: FakeLive(run["settings"], uncertain=True))
    run2 = eng2.start_run("live")
    eng2.tick()
    r = eng2.get_run(run2["id"])
    assert r["status"] == "halted" and "Onzeker" in r["halt_reason"]
    assert any(k == "alert" for _, _, k in hass2.sent)


# --------------------------------------------------------------------------- broker-API's (nagebootst)
class FakeResp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls, self.headers = routes, [], {}

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": data})
        for (m, frag), resp in self.routes.items():
            if m == method and frag in url:
                return resp(data) if callable(resp) else resp
        return FakeResp(404, {"error": "not found"})


def test_bitvavo_signature_and_orders():
    s = _settings(bitvavo_operator_id=4242)
    sess = FakeSession({
        ("GET", "/markets"): FakeResp(200, {"market": "BTC-EUR", "status": "trading", "quantityDecimals": 6,
                                            "pricePrecision": 5}),
        ("GET", "/balance"): FakeResp(200, [{"symbol": "BTC", "available": "0.0105"}]),
        ("POST", "/order"): lambda body: FakeResp(200, {
            "orderId": "o1", "status": "filled", "filledAmount": "0.0100", "filledAmountQuote": "500.00",
            "feePaid": "1.25", "feeCurrency": "EUR"}),
    })
    b = BitvavoBroker("key", "secret", s, dry_run=False, session=sess, sleep=lambda x: None)
    f = b.execute(Order("crypto", "BTC", "buy", ref_price=55000, fx=1.1, eur=500.009))
    post = [c for c in sess.calls if c["method"] == "POST"][0]
    body = json.loads(post["body"])
    assert body["amountQuote"] == "500.00" and body["operatorId"] == 4242 and body["orderType"] == "market"
    h = post["headers"]
    msg = f"{h['Bitvavo-Access-Timestamp']}POST/v2/order{post['body']}"
    assert h["Bitvavo-Access-Signature"] == hmac.new(b"secret", msg.encode(), hashlib.sha256).hexdigest()
    assert f["price_eur"] == pytest.approx(50000) and f["price"] == pytest.approx(55000) and f["fee_eur"] == 1.25
    b.execute(Order("crypto", "BTC", "sell", ref_price=55000, fx=1.1, qty=0.02))
    sell = json.loads([c for c in sess.calls if c["method"] == "POST"][-1]["body"])
    assert sell["amount"] == "0.0105"                            # nooit meer dan het vrije saldo
    with pytest.raises(BrokerError, match="short"):
        b.execute(Order("crypto", "BTC", "sell", ref_price=1, fx=1, qty=1, intent="open_short"))
    dry = BitvavoBroker("key", "secret", s, dry_run=True, session=FakeSession({}))
    assert dry.execute(Order("crypto", "BTC", "buy", ref_price=100, fx=1, eur=50))["dry_run"] is True


def test_trading212_auth_quantity_and_mapping(db):
    s = _settings(trading212_env="demo")
    sess = FakeSession({
        ("GET", "/equity/metadata/instruments"): FakeResp(200, [
            {"ticker": "AAPL_US_EQ", "shortName": "AAPL", "currencyCode": "USD"},
            {"ticker": "AAPLl_EQ", "shortName": "AAPL", "currencyCode": "GBX"}]),
        ("POST", "/equity/orders/market"): lambda body: FakeResp(200, {"id": 7, "status": "FILLED",
                                                                       "filledQuantity": abs(json.loads(body)["quantity"]),
                                                                       "filledValue": 300.0}),
        ("GET", "/equity/account/cash"): FakeResp(200, {"free": 123.45}),
    })
    b = Trading212Broker("abc", "xyz", s, dry_run=False, db=db, session=sess, sleep=lambda x: None)
    assert b.base == "https://demo.trading212.com/api/v0"
    assert b.auth_header() == "Basic " + base64.b64encode(b"abc:xyz").decode()
    assert b.free_cash_eur() == 123.45
    f = b.execute(Order("stocks", "AAPL", "buy", ref_price=200, fx=1.0, eur=300))
    body = json.loads([c for c in sess.calls if c["method"] == "POST"][0]["body"])
    assert body == {"ticker": "AAPL_US_EQ", "quantity": 1.5}
    assert f["qty"] == 1.5 and f["price"] == pytest.approx(200)
    b.execute(Order("stocks", "AAPL", "sell", ref_price=200, fx=1.0, qty=1.5))
    assert json.loads([c for c in sess.calls if c["method"] == "POST"][-1]["body"])["quantity"] == -1.5
    old = Trading212Broker("oldkey", "", s, dry_run=True, db=db, session=FakeSession({}))
    assert old.auth_header() == "oldkey"


# --------------------------------------------------------------------------- web
def test_trading_api(tmp_path):
    cfg = load_config(str(tmp_path))
    m = MarketMonitor(cfg)
    m.trading.fx = FakeFx()
    c = create_app(m).test_client()
    o = c.get("/api/trading").json
    assert o["settings"]["budget_crypto_eur"] == 1000 and o["schema"]["fields"] and o["confirm_text"] == CONFIRM_TEXT
    assert o["allow_live"] is False and o["runs"] == []
    bad = c.post("/api/trading/settings", json={"risk_per_trade_pct": 99})
    assert bad.status_code == 400 and "risk_per_trade_pct" in bad.json["errors"]
    ok = c.post("/api/trading/settings", json={"risk_per_trade_pct": 2, "allowed_tickers": "btc,eth"}).json
    assert ok["ok"] and ok["settings"]["allowed_tickers"] == ["BTC", "ETH"]
    r = c.post("/api/trading/runs", json={"mode": "paper", "name": "Test A", "hours": 3}).json
    assert r["ok"] and r["run"]["name"] == "Test A" and r["run"]["ends_at"]
    rid = r["run"]["id"]
    assert c.post("/api/trading/runs", json={"mode": "live"}).status_code == 400
    d = c.get(f"/api/trading/runs/{rid}").json
    assert d["settings"]["risk_per_trade_pct"] == 2 and d["stats"]["start_eur"] > 0
    assert c.post(f"/api/trading/runs/{rid}/delete").status_code == 400      # draait nog
    assert c.post(f"/api/trading/runs/{rid}/stop", json={"close": True}).json["run"]["status"] == "stopped"
    c.post("/api/trading/settings", json={"risk_per_trade_pct": 3})
    assert c.post(f"/api/trading/settings/from/{rid}").json["settings"]["risk_per_trade_pct"] == 2
    assert c.post("/api/trading/settings/reset").json["settings"]["risk_per_trade_pct"] == 1.0
    chk = c.post("/api/trading/check").json["results"]
    assert all(not x["ok"] for x in chk)                                   # geen sleutels ingevuld
    assert c.post(f"/api/trading/runs/{rid}/delete").json["ok"]
    assert c.get(f"/api/trading/runs/{rid}").status_code == 404
