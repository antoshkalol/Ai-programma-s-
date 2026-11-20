"""Tests voor het tabblad Analyse: historische trefkans, data ophalen, de analyse door Claude en de controle."""

import json
import time

import pytest

from market_monitor.analyzer import DeepAnalyzer, MarketData
from market_monitor.app import MarketMonitor
from market_monitor.config import load_config
from market_monitor.recommend import Recommender
from market_monitor.technical import hit_rate
from market_monitor.web import create_app

from test_recommend import _seed, series


# --------------------------------------------------------------------------- trefkans
def test_hit_rate():
    up = [100 * 1.01 ** i for i in range(300)]
    long = hit_rate(up, 3, 2, 20, "long")
    assert long["win_rate"] == 100 and long["losses"] == 0 and long["n"] == 250
    short = hit_rate(up, 3, 2, 20, "short")
    assert short["win_rate"] == 0 and short["wins"] == 0
    flat = hit_rate([100.0] * 120, 3, 2, 20, "long")
    assert flat["timeouts"] == flat["n"] and flat["win_rate"] is None
    assert hit_rate(up[:30], 3, 2) is None and hit_rate(up, 0, 2) is None


# --------------------------------------------------------------------------- data ophalen
class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.text = data, status, json.dumps(data)

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))


class Sess:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def request(self, method, url, timeout=None, **kw):
        self.calls.append(url)
        for frag, resp in self.routes.items():
            if frag in url:
                return resp
        return Resp({}, 404)


def test_market_data_stocks(cfg, db, monkeypatch):
    md = MarketData(cfg, db, session=Sess({}))
    dates, closes = series(0.002, n=260, seed=4)
    parsed = {"series": list(zip(dates, closes)), "last_price": closes[-1], "prev_close": closes[-2],
              "name": "Voorbeeld", "currency": "USD", "provider": "yahoo"}
    monkeypatch.setattr(md._prices, "fetch_one", lambda t: parsed)
    assert md.ensure("stocks", "XYZ") is None
    assert len(md.closes("stocks", "XYZ")) == 260
    monkeypatch.setattr(md._prices, "fetch_one", lambda t: None)
    err = md.ensure("stocks", "NOPE")
    assert err and "Geen koersen gevonden voor NOPE" in err


def test_market_data_crypto_search_and_history(cfg, db):
    ms = 1_700_000_000_000
    chart = {"prices": [[ms + i * 86_400_000, 1.0 + i * 0.01] for i in range(220)]}
    sess = Sess({"/search?query=newc": Resp({"coins": [{"id": "new-coin", "symbol": "NEWC", "name": "New Coin",
                                                         "market_cap_rank": 250},
                                                        {"id": "fake", "symbol": "NEWC", "market_cap_rank": None}]}),
                 "/coins/new-coin/market_chart": Resp(chart),
                 "/simple/price": Resp({"new-coin": {"usd": 3.21, "usd_24h_change": 7.0}})})
    md = MarketData(cfg, db, session=sess)
    assert md.ensure("crypto", "NEWC") is None
    assert len(md.closes("crypto", "NEWC")) == 220
    meta = db.query_one("SELECT * FROM price_meta WHERE market='crypto' AND ticker='NEWC'")
    assert meta["last_price"] == 3.21 and meta["name"] == "New Coin"
    assert db.get_state("coingecko_extra")["NEWC"]["id"] == "new-coin"
    md2 = MarketData(cfg, db, session=Sess({"/search": Resp({"coins": []})}))
    assert "niet gevonden" in md2.ensure("crypto", "ZZZZ")


# --------------------------------------------------------------------------- analyse
class NoFetch(MarketData):
    def ensure(self, market, ticker):
        return None if len(self.closes(market, ticker)) >= 60 else f"Te weinig koershistorie voor {ticker}"


def ai_result(action="kopen", levels=(1, 2, 3, 4, 5), prob=72, scen=(30, 50, 40)):
    lo, hi, stop, t1, t2 = levels
    return {"headline": "Kop", "summary": "Samenvatting",
            "recommendation": {"action": action, "timeframe": "2-6 weken", "entry_low": lo, "entry_high": hi,
                               "stop": stop, "target1": t1, "target2": t2, "probability_target1": prob,
                               "confidence": "gemiddeld", "reasoning": "Omdat"},
            "scenarios": [{"name": n, "probability": p, "target": 1.0, "description": "d"}
                          for n, p in zip(("negatief", "positief", "basis"), scen)],
            "trend": {"short": "a", "medium": "b", "long": "c"}, "momentum": "m", "volatility": "v",
            "levels": [{"price": 1.0, "type": "steun", "strength": "sterk", "note": ""},
                       {"price": -5, "type": "steun", "strength": "zwak", "note": "fout"}],
            "patterns": ["p"], "indicators": [], "catalysts": [], "risks": ["r"], "invalidation": "i", "watch": []}


class FakeAI:
    def __init__(self, result=None, fail=False):
        self.result, self.fail, self.calls = result, fail, []

    def ask_json(self, system, text, schema, effort=None, max_tokens=16000, model=None, usage=None, stream=False):
        self.calls.append({"text": text, "model": model, "effort": effort, "stream": stream, "max_tokens": max_tokens})
        if self.fail:
            raise RuntimeError("AI weg")
        usage.update({"model": model, "input": 10000, "output": 5000, "cache_read": 0, "cache_write": 0})
        return self.result


def _an(cfg, db, ai):
    cfg.anthropic_api_key = "sk-test"
    return DeepAnalyzer(cfg, db, Recommender(cfg, db), ai, data=NoFetch(cfg, db))


def test_analysis_with_inconsistent_levels_falls_back(cfg, db):
    _seed(db, "stocks", "AAA", 0.004, 76, seed=1)
    db.execute("INSERT INTO news(id, market, title, fetched_at, published_at, tickers, credibility, corroboration)"
               " VALUES ('n1','stocks','AAA wint grote order', datetime('now'), strftime('%Y-%m-%dT%H:%M:%SZ','now'),"
               " '[\"AAA\"]', 0.9, 2)")
    ai = FakeAI(ai_result(prob=120))
    an = _an(cfg, db, ai)
    r = an.start("stocks", "aaa", wait=True)
    assert r["status"] == "done" and r["ticker"] == "AAA" and r["model"] == "claude-opus-5-5"
    assert ai.calls[0]["model"] == "claude-opus-5-5" and ai.calls[0]["effort"] == "high"
    assert ai.calls[0]["stream"] is True and ai.calls[0]["max_tokens"] == 32000
    text = ai.calls[0]["text"]
    for key in ("scorekaart", "historische_trefkans_20_dagen", "dagkoersen_60", "weekkoersen_1jaar", "koopscore",
                "AAA wint grote order"):
        assert key in text
    rec = r["ai"]["recommendation"]
    p = r["local"]["plan"]
    assert "niet consistent" in r["error"]
    assert rec["stop"] < rec["entry_low"] <= rec["entry_high"] < rec["target1"] <= rec["target2"]
    if p["direction"] == "long":
        assert rec["stop"] == pytest.approx(p["stop"])
    assert rec["probability_target1"] == 95 and rec["rr"] > 0
    assert [s["name"] for s in r["ai"]["scenarios"]] == ["positief", "basis", "negatief"]
    assert sum(s["probability"] for s in r["ai"]["scenarios"]) in (99, 100, 101)
    assert all(lv["price"] > 0 for lv in r["ai"]["levels"])
    assert r["cost_usd"] == pytest.approx((10000 * 4 + 5000 * 20) / 1e6)
    loc = r["local"]
    assert loc["hit_rates"]["long"]["n"] > 0 and loc["returns"]["20d"] is not None and loc["chart"]["close"]


def test_analysis_short_cache_force_and_listing(cfg, db):
    _seed(db, "crypto", "BBB", -0.005, 22, seed=2)
    price = db.query_one("SELECT close FROM prices WHERE ticker='BBB' ORDER BY date DESC LIMIT 1")["close"]
    ai = FakeAI(ai_result("short", (price, price * 1.02, price * 1.08, price * 0.9, price * 0.8), prob=41))
    an = _an(cfg, db, ai)
    r = an.start("crypto", "BBB", model="claude-sonnet-5-5", effort="xhigh", wait=True)
    rec = r["ai"]["recommendation"]
    assert r["error"] is None and rec["action"] == "short" and rec["stop"] == pytest.approx(price * 1.08)
    assert ai.calls[0]["model"] == "claude-sonnet-5-5" and ai.calls[0]["effort"] == "xhigh"
    again = an.start("crypto", "BBB", model="claude-sonnet-5-5")
    assert again["cached"] is True and again["id"] == r["id"] and len(ai.calls) == 1
    new = an.start("crypto", "BBB", model="claude-sonnet-5-5", force=True, wait=True)
    assert new["id"] != r["id"] and len(ai.calls) == 2
    recent = an.recent()
    assert recent[0]["ticker"] == "BBB" and recent[0]["action"] == "short" and recent[0]["probability"] == 41
    assert an.delete(r["id"]) and an.get(r["id"]) is None


def test_analysis_errors_and_without_ai(cfg, db):
    an = _an(cfg, db, FakeAI(fail=True))
    with pytest.raises(ValueError):
        an.start("stocks", "")
    with pytest.raises(ValueError):
        an.start("forex", "EURUSD")
    with pytest.raises(ValueError):
        an.start("stocks", "AA<script>")
    bad = an.start("stocks", "NODATA", wait=True)
    assert bad["status"] == "error" and "Te weinig koershistorie" in bad["error"]
    _seed(db, "stocks", "CCC", 0.003, 60, seed=3)
    failed = an.start("stocks", "CCC", wait=True)
    assert failed["status"] == "error" and "AI weg" in failed["error"]
    cfg.anthropic_api_key = ""
    local = DeepAnalyzer(cfg, db, Recommender(cfg, db), FakeAI(), data=NoFetch(cfg, db)).start("stocks", "CCC",
                                                                                                 force=True, wait=True)
    assert local["status"] == "done" and local["ai"] is None and "Geen AI-sleutel" in local["error"]
    db.execute("UPDATE user_analyses SET status='running' WHERE id=?", (local["id"],))
    DeepAnalyzer(cfg, db, Recommender(cfg, db), None, data=NoFetch(cfg, db))      # herstart
    assert an.get(local["id"])["status"] == "error"


def test_analysis_api(tmp_path):
    cfg = load_config(str(tmp_path))
    cfg.anthropic_api_key = "sk-test"
    m = MarketMonitor(cfg)
    _seed(m.db, "stocks", "AAA", 0.004, 76, seed=1)
    m.analyzer.data = NoFetch(cfg, m.db)
    m.analyzer.summarizer = FakeAI(ai_result())
    c = create_app(m).test_client()
    meta = c.get("/api/analysis").json
    assert meta["models"]["claude-opus-5-5"] == "Opus 5.5" and meta["recent"] == [] and meta["ai_available"]
    assert c.post("/api/analysis", json={"market": "stocks", "ticker": ""}).status_code == 400
    r = c.post("/api/analysis", json={"market": "stocks", "ticker": "AAA"}).json
    aid = r["analysis"]["id"]
    for _ in range(100):
        a = c.get(f"/api/analysis/{aid}").json
        if a["status"] != "running":
            break
        time.sleep(0.05)
    assert a["status"] == "done" and a["ai"]["recommendation"]["action"] == "kopen" and a["model_label"] == "Opus 5.5"
    assert c.get("/api/analysis").json["recent"][0]["id"] == aid
    assert c.get("/api/analysis/999").status_code == 404
    assert c.post(f"/api/analysis/{aid}/delete").json["ok"]


# --------------------------------------------------------------------------- streaming naar de API
class _Stream:
    def __init__(self, resp):
        self.resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.resp


class _Msgs:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def stream(self, **kw):
        self.calls.append(("stream", kw))
        return _Stream(self.resp)

    def create(self, **kw):
        self.calls.append(("create", kw))
        return self.resp


class _Client:
    def __init__(self, resp):
        self.messages = _Msgs(resp)
        self.beta = type("B", (), {"messages": self.messages})()
        self.options = None

    def with_options(self, **kw):
        self.options = kw
        return self


def test_ask_json_streams_with_fallback_and_reports_usage(cfg, db):
    from types import SimpleNamespace

    from market_monitor.summary import Summarizer

    cfg.anthropic_api_key = "sk-test"
    usage_obj = SimpleNamespace(input_tokens=100, output_tokens=50, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0)
    resp = SimpleNamespace(stop_reason="end_turn", model="claude-opus-5-5", usage=usage_obj,
                           content=[SimpleNamespace(type="text", text='{"ok": true}')])
    s = Summarizer(cfg, db)
    s._client = _Client(resp)
    usage = {}
    assert s.ask_json("sys", "tekst", {"type": "object"}, model="claude-opus-5-5", usage=usage, max_tokens=32000,
                      stream=True) == {"ok": True}
    kind, kw = s._client.messages.calls[0]
    assert kind == "stream" and kw["fallbacks"] == "default" and kw["max_tokens"] == 32000
    assert s._client.options["timeout"] >= 600 and usage["input"] == 100 and usage["output"] == 50
    s._client = _Client(resp)
    s.ask_json("sys", "tekst", {"type": "object"}, model="claude-haiku-5-5", stream=True)
    kind, kw = s._client.messages.calls[0]
    assert kind == "stream" and "fallbacks" not in kw
    s._client = _Client(SimpleNamespace(stop_reason="max_tokens", model="x", usage=usage_obj, content=[]))
    with pytest.raises(RuntimeError, match="lagere diepgang"):
        s.ask_json("sys", "tekst", {"type": "object"}, model="claude-opus-5-5", stream=True)
