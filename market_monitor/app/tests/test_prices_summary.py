import json

import pytest

from market_monitor.app import MarketMonitor
from market_monitor.config import load_config
from market_monitor.db import ago, iso, utcnow
from market_monitor.sources import prices
from market_monitor.summary import OUTPUT_SCHEMA, Summarizer, build_overview
from market_monitor.web import create_app


class Resp:
    def __init__(self, status=200, js=None, text=""):
        self.status_code, self._js, self.text = status, js, text
        self.ok = status < 400

    def json(self):
        return self._js


NASDAQ_JSON = {"data": {"symbol": "AAPL", "tradesTable": {"rows": [
    {"date": "11/07/2026", "close": "$227.48", "open": "$225.00"},
    {"date": "11/06/2026", "close": "$1,225.10", "open": "$224.00"},
    {"date": "bogus", "close": "$1"},
]}}, "status": {"rCode": 200}}
TWELVE_JSON = {"meta": {"symbol": "AAPL", "currency": "USD"}, "status": "ok",
               "values": [{"datetime": "2026-11-07", "close": "227.48"}, {"datetime": "2026-11-06", "close": "225.10"}]}


def test_parse_nasdaq_and_twelve():
    n = prices.parse_nasdaq(NASDAQ_JSON)
    assert n["series"] == [("2026-11-06", 1225.10), ("2026-11-07", 227.48)] and n["last_price"] == 227.48
    t = prices.parse_twelve(TWELVE_JSON)
    assert t["series"][-1] == ("2026-11-07", 227.48) and t["prev_close"] == 225.10
    assert prices.parse_twelve({"status": "error", "code": 401}) is None
    assert prices.parse_nasdaq({"data": None}) is None


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def request(self, method, url, timeout=None, **kw):
        self.calls.append(url)
        for key, resp in self.routes.items():
            if key in url:
                return resp(url) if callable(resp) else resp
        return Resp(404)


def test_falls_back_and_blocks_rate_limited_source(cfg, db):
    s = FakeSession({"finance.yahoo.com": Resp(429), "api.nasdaq.com": Resp(js=NASDAQ_JSON)})
    src = prices.PriceSource(cfg, db, s, lambda: ["AAPL", "MSFT"])
    assert src.run() == 2
    yahoo_calls = [u for u in s.calls if "yahoo" in u]
    assert len(yahoo_calls) == 1  # na de 429 wordt Yahoo 30 minuten overgeslagen
    state = db.get_state("price_sources")
    assert state["used"] == {"nasdaq": 2} and "429" in state["errors"]["yahoo"]
    assert db.query_one("SELECT COUNT(*) n FROM prices WHERE market='stocks' AND ticker='MSFT'")["n"] == 2


def test_twelve_data_first_when_key_set(cfg, db):
    cfg.stock_price_api_key = "td-key"
    s = FakeSession({"api.twelvedata.com": Resp(js=TWELVE_JSON)})
    src = prices.PriceSource(cfg, db, s, lambda: ["AAPL"])
    assert src.run() == 1
    assert s.calls[0].startswith("https://api.twelvedata.com/time_series?symbol=AAPL") and "apikey=td-key" in s.calls[0]


def test_all_sources_failing_reports_details(cfg, db):
    s = FakeSession({"finance.yahoo.com": Resp(429), "api.nasdaq.com": Resp(403),
                     "stooq.com": Resp(text="Exceeded the daily hits limit")})
    src = prices.PriceSource(cfg, db, s, lambda: ["AAPL"])
    with pytest.raises(RuntimeError) as exc:
        src.run()
    msg = str(exc.value)
    assert "yahoo: te veel verzoeken (429)" in msg and "nasdaq: geweigerd (403)" in msg and "stooq" in msg


def test_non_us_tickers_only_use_yahoo(cfg, db):
    s = FakeSession({"finance.yahoo.com": Resp(500)})
    src = prices.PriceSource(cfg, db, s, lambda: ["ADYEN.AS"])
    with pytest.raises(RuntimeError):
        src.run()
    assert all("yahoo" in u for u in s.calls)


# --------------------------------------------------------------------------- samenvatting
def _data(scores=None, context=None, rec=None, news=None):
    return {"news": news or [], "posts": [], "filtered": 2, "trades": [], "scores": scores or [],
            "context": context or [], "recommendation": rec}


def test_overview_always_names_best_buy():
    scores = [{"ticker": "NVDA", "score": 78, "prob_up": 0.68, "timing": "Stijgende trend",
               "reasons": ["3 insider(s) kochten $4,2 mln in 30 dagen"]}]
    ctx = [{"ticker": "NVDA", "n": 3, "total": 4.2e6, "types": "insider"},
           {"ticker": "AAPL", "n": 2, "total": 9e5, "types": "insider,politician"}]
    rec = {"direction": "long", "ticker": "NVDA", "entry_low": 120, "entry_high": 122, "target1": 135, "stop": 114}
    text = build_overview("stocks", _data(scores, ctx, rec), "positief", {"NVDA": [0.6, 0.4]})
    assert "weinig nieuw nieuws" in text
    assert "NVDA (positief)" in text
    assert "NVDA (3 insiders, $4,2 mln)" in text and "AAPL (insider + politicus, $900k)" in text
    assert "Beste aandeel om nu te kopen volgens het model: NVDA (koopscore 78" in text
    assert "LONG NVDA: instap $120,00-$122,00, doel $135,00, stop $114,00" in text
    none = build_overview("crypto", _data([{"ticker": "BTC", "score": 48, "prob_up": 0.5, "timing": "Neutraal"}]),
                          "neutraal", {})
    assert "geen munt met een duidelijk koopsignaal" in none


def test_ai_schema_and_summary_now_api(tmp_path):
    assert "overview" in OUTPUT_SCHEMA["required"]
    m = MarketMonitor(load_config(str(tmp_path)))
    m.db.execute("INSERT INTO scores_latest(market, ticker, computed_at, score, prob_up, timing, reasons)"
                 " VALUES ('stocks','NVDA',?,77,0.67,'Stijgende trend',?)",
                 (iso(utcnow()), json.dumps(["Insiders kopen"])))
    c = create_app(m).test_client()
    r = c.post("/api/summary/now?market=stocks").json
    assert r["ok"] and "NVDA" in r["content"]["overview"]
    assert c.get("/api/summaries?market=stocks").json[0]["content"]["overview"] == r["content"]["overview"]
    rec = c.get("/api/recommendation?market=stocks").json
    assert rec["recommendation"] is None  # geen koershistorie
    assert rec["fallback"]["top"]["ticker"] == "NVDA" and rec["fallback"]["price_days"] == 0
    assert "NVDA" in rec["overview"]


def test_gather_includes_week_context(cfg, db):
    db.insert_trades([
        {"id": "a", "source": "sec_form4", "actor_name": "A", "actor_type": "insider", "ticker": "NVDA",
         "tx_type": "buy", "value_usd": 1e6, "filed_at": ago(days=2)},
        {"id": "b", "source": "senate", "actor_name": "B", "actor_type": "politician", "ticker": "NVDA",
         "tx_type": "buy", "value_usd": 5e5, "filed_at": ago(days=3)},
        {"id": "c", "source": "sec_form4", "actor_name": "C", "actor_type": "insider", "ticker": "OLD",
         "tx_type": "buy", "value_usd": 9e9, "filed_at": ago(days=20)},
    ])
    data = Summarizer(cfg, db).gather("stocks", ago(hours=1), iso(utcnow()))
    assert [(c["ticker"], c["n"]) for c in data["context"]] == [("NVDA", 2)]
