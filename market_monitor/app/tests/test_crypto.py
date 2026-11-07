import json
from datetime import timedelta

from market_monitor.app import MarketMonitor
from market_monitor.config import load_config
from market_monitor.db import ago, iso, utcnow
from market_monitor.filtering import NewsIngestor, clickbait_score
from market_monitor.scoring import CryptoScoringEngine, fmt_num, fmt_price, institutional_component
from market_monitor.sources import btc_whales, coingecko, news, sec_treasury
from market_monitor.tickers import TickerExtractor
from market_monitor.web import create_app


class Resp:
    def __init__(self, text=None, js=None, status=200):
        self.text, self._js, self.status_code, self.ok = text, js, status, status < 400
        self.content = (text or "").encode()

    def json(self):
        return self._js if self._js is not None else json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeSession:
    def __init__(self, routes):
        self.routes, self.urls = routes, []

    def request(self, method, url, timeout=None, **kw):
        self.urls.append((url, kw.get("headers") or {}))
        for key, resp in self.routes.items():
            if key in url:
                return resp() if callable(resp) else resp
        raise AssertionError("onverwachte url " + url)


# --------------------------------------------------------------------------- CoinGecko
def test_parse_markets_excludes_stablecoins_and_duplicates(fixture_text):
    coins = coingecko.parse_markets(json.loads(fixture_text("coingecko_markets.json")))
    by = {c["symbol"]: c for c in coins}
    assert [c["symbol"] for c in coins] == ["BTC", "ETH", "USDT", "SOL", "WBTC"]  # imposter-SOL valt af
    assert by["USDT"]["excluded"] and by["WBTC"]["excluded"] and not by["BTC"]["excluded"]
    assert by["SOL"]["id"] == "solana" and by["SOL"]["chg_1h"] == 2.5


def test_coingecko_markets_run_stores_snapshots_and_meta(cfg, db, fixture_text):
    cfg.coingecko_api_key = "demo-key"
    s = FakeSession({"coins/markets": Resp(text=fixture_text("coingecko_markets.json"))})
    assert coingecko.CoinGeckoMarkets(cfg, db, s).run() == 3
    assert s.urls[0][1]["x-cg-demo-api-key"] == "demo-key"
    assert set(db.get_state("coingecko_map")) == {"BTC", "ETH", "SOL"}
    meta = db.query_one("SELECT * FROM price_meta WHERE market='crypto' AND ticker='SOL'")
    assert meta["last_price"] == 210.0 and meta["rank"] == 5
    assert round(meta["prev_close"], 2) == round(210 / 1.06, 2)
    assert db.query_one("SELECT COUNT(*) n FROM crypto_snapshots")["n"] == 3


def test_parse_chart_daily():
    day = 86_400_000
    data = {"prices": [[1762300800000, 100.0], [1762300800000 + 3600_000, 101.0], [1762300800000 + day, 105.0]]}
    assert coingecko.parse_chart(data) == [("2025-11-05", 101.0), ("2025-11-06", 105.0)]


def test_coingecko_rate_limit_is_reported(cfg, db):
    import pytest

    s = FakeSession({"coins/markets": Resp(status=429, text="{}")})
    with pytest.raises(RuntimeError, match="limiet"):
        coingecko.CoinGeckoMarkets(cfg, db, s).run()


# --------------------------------------------------------------------------- BTC-walvissen
def test_whale_transfers_ignore_change_and_self_moves(fixture_text):
    block = json.loads(fixture_text("btc_block.json"))
    ws = btc_whales.whale_transfers(block, min_btc=500)
    assert [w["hash"] for w in ws] == ["whale1"]
    assert ws[0]["btc"] == 1200.0  # wisselgeld (299,9 BTC terug naar afzender) telt niet
    assert ws[0]["from"].startswith("bc1qse") and "..." in ws[0]["to"]


def test_btc_whale_source_run(cfg, db, fixture_text):
    db.execute("INSERT INTO price_meta(market, ticker, last_price) VALUES ('crypto', 'BTC', 100000)")
    cfg.crypto_whale_min_usd = 50_000_000  # = 500 BTC
    s = FakeSession({
        "latestblock": Resp(js={"hash": "00000000000000000001aaaa", "height": 920001}),
        "rawblock": Resp(text=fixture_text("btc_block.json")),
    })
    src = btc_whales.BtcWhaleSource(cfg, db, s)
    assert src.run() == 1
    t = db.query_one("SELECT * FROM trades WHERE market='crypto'")
    assert t["actor_type"] == "whale" and t["value_usd"] == 120_000_000 and t["tx_type"] == "other"
    assert "1.200 BTC" in t["note"]
    assert src.run() == 0  # zelfde blokhoogte: niets nieuws


# --------------------------------------------------------------------------- bedrijfsaankopen (8-K)
def test_parse_hits_and_amounts(fixture_text):
    hits = sec_treasury.parse_hits(json.loads(fixture_text("efts_hits.json")))
    assert hits[0]["company"] == "Example Treasury Corp" and hits[0]["company_ticker"] == "EXTC"
    assert hits[0]["cik"] == "9999999" and hits[0]["file"] == "ex99-1.htm"
    text = sec_treasury.html_to_text(fixture_text("form8k.html"))
    amounts = sec_treasury.parse_amounts(text, "BTC")
    assert amounts[0] == {"side": "buy", "qty": 4250.0, "value": 425.3e6}
    assert amounts[1] == {"side": "sell", "qty": 120.0, "value": 120 * 101_000}  # prijs per munt x aantal
    assert len(amounts) == 2  # 'held an aggregate of 52,000 bitcoins' is geen aankoop


def test_treasury_source_run(cfg, db, fixture_text):
    s = FakeSession({
        "efts.sec.gov": Resp(text=fixture_text("efts_hits.json")),
        "ex99-1.htm": Resp(text=fixture_text("form8k.html")),
        "risk.htm": Resp(text="<p>Bitcoin is volatile. We have not acquired any digital assets.</p>"),
    })
    src = sec_treasury.SecTreasurySource(cfg, db, s)
    assert src.run() == 2
    rows = db.query("SELECT * FROM trades WHERE market='crypto' ORDER BY id")
    assert rows[0]["actor_name"] == "Example Treasury Corp" and rows[0]["ticker"] == "BTC"
    assert rows[0]["tx_type"] == "buy" and rows[0]["value_usd"] == 425.3e6
    assert all(h.get("User-Agent") == "Test test@example.com" for _, h in s.urls)
    assert src.run() == 0


# --------------------------------------------------------------------------- nieuws en tickers
def test_crypto_ticker_extraction(db):
    db.set_state("coingecko_map", {"HYPE": {"name": "Hyperliquid"}, "PENGU": {"name": "Pudgy Penguins"}})
    ex = TickerExtractor(db, ["BTC"], "crypto")
    assert ex.extract("Bitcoin and Ether rally while $SOL lags") == ["SOL", "BTC", "ETH"]
    assert ex.extract("Pudgy Penguins token jumps; USDT supply grows") == ["PENGU"]
    assert ex.extract("$AAPL and $TSLA rise") == []  # aandelen tellen niet in crypto
    assert ex.extract("NFT and DEFI hype") == []


def test_crypto_clickbait():
    assert clickbait_score("Best crypto to buy now: this presale could be the next 1000x gem") >= 0.7
    assert clickbait_score("XRP price prediction: could hit $10?") >= 0.5
    assert clickbait_score("SEC approves spot ether ETF options") < 0.2


def test_news_ids_are_separate_per_market(cfg, db):
    ing = NewsIngestor(cfg, db, {"stocks": TickerExtractor(db, [], "stocks"),
                                 "crypto": TickerExtractor(db, [], "crypto")})
    item = {"id": "abc", "source": "Reuters", "domain": "reuters.com", "title": "Coinbase shares jump as bitcoin rallies",
            "url": "https://r/1", "published_at": iso(utcnow())}
    assert ing([item], market="stocks") == 1
    assert ing([item], market="crypto") == 1
    rows = {r["market"]: r for r in db.query("SELECT * FROM news")}
    assert json.loads(rows["crypto"]["tickers"]) == ["BTC"]


def test_crypto_feeds_and_queries(cfg):
    src = news.RssSource(cfg, None, None, "crypto")
    assert any("coindesk" in u for _, u in src.feeds())
    gn = news.GoogleNewsSource(cfg, None, None, "crypto")
    assert "Bitcoin crypto" in gn.queries() and "bitcoin" in gn.queries()


# --------------------------------------------------------------------------- score
def test_institutional_component():
    buys = [{"actor_name": "Example Treasury Corp", "tx_type": "buy", "value_usd": 400e6, "ticker": "BTC"}]
    proxy = [{"actor_name": "Sen. Example", "tx_type": "buy", "value_usd": 75000, "ticker": "IBIT"}]
    s1, r1 = institutional_component(buys, [])
    s2, r2 = institutional_component(buys, proxy)
    assert 0.5 < s1 < s2 <= 1
    assert "Example Treasury Corp" in r1[0] and "IBIT" in r2[1]
    assert institutional_component([], []) == (0.0, [])


def test_crypto_scoring_engine(cfg, db):
    db.set_state("coingecko_map", {"BTC": {"id": "bitcoin"}, "ETH": {"id": "ethereum"}})
    start = utcnow() - timedelta(days=90)
    db.executemany("INSERT INTO prices(market, ticker, date, close) VALUES ('crypto',?,?,?)",
                   [("BTC", (start + timedelta(days=i)).strftime("%Y-%m-%d"), 60000 + i * 400) for i in range(90)]
                   + [("ETH", (start + timedelta(days=i)).strftime("%Y-%m-%d"), 4000 - i * 15) for i in range(90)])
    db.execute("INSERT INTO price_meta(market, ticker, last_price, chg_24h, chg_7d) VALUES ('crypto','BTC',95600,1.5,4.2)")
    db.insert_trades([
        {"id": "c1", "market": "crypto", "source": "sec_8k", "actor_name": "Example Treasury Corp", "actor_type": "company",
         "ticker": "BTC", "tx_type": "buy", "value_usd": 400e6, "filed_at": ago(days=2)},
        {"id": "p1", "source": "senate", "actor_name": "Casey Example", "actor_type": "politician", "ticker": "IBIT",
         "tx_type": "buy", "value_usd": 75000, "filed_at": ago(days=3)},
    ])
    eng = CryptoScoringEngine(cfg, db)
    assert eng.candidate_tickers() == ["BTC", "ETH"]
    assert eng.run() == 2
    rows = {r["ticker"]: r for r in db.query("SELECT * FROM scores_latest WHERE market='crypto'")}
    assert rows["BTC"]["score"] > 60 > rows["ETH"]["score"]
    comps = json.loads(rows["BTC"]["components"])
    assert set(comps) >= {"institutional", "news", "momentum"} and comps["institutional"] > 0.5
    reasons = json.loads(rows["BTC"]["reasons"])
    assert any("IBIT" in r for r in reasons) and any("24 uur" in r for r in reasons)
    assert db.query_one("SELECT COUNT(*) n FROM scores_latest WHERE market='stocks'")["n"] == 0


def test_formatting_dutch():
    assert fmt_num(7019.4) == "7.019" and fmt_price(467.61) == "$467,61" and fmt_price(0.0000182) == "$0,00001820"


# --------------------------------------------------------------------------- API
def test_crypto_api_includes_proxy_trades(tmp_path):
    m = MarketMonitor(load_config(str(tmp_path)))
    m.db.insert_trades([
        {"id": "p1", "source": "senate", "actor_name": "Casey Example", "actor_role": "Senator",
         "actor_type": "politician", "ticker": "IBIT", "tx_type": "buy", "value_low": 15001, "value_high": 50000,
         "value_usd": 32500, "tx_date": ago(days=10)[:10], "filed_at": ago(days=1)},
        {"id": "s1", "source": "sec_form4", "actor_name": "Jane", "actor_type": "insider", "ticker": "AAPL",
         "tx_type": "buy", "value_usd": 900000, "filed_at": ago(days=1)},
        {"id": "w1", "market": "crypto", "source": "btc_chain", "actor_name": "Wallet x", "actor_type": "whale",
         "ticker": "BTC", "tx_type": "other", "value_usd": 9e7, "filed_at": ago(hours=1)},
    ])
    c = create_app(m).test_client()
    rows = c.get("/api/trades?market=crypto").json
    assert {r["id"] for r in rows} == {"p1", "w1"}
    assert next(r for r in rows if r["id"] == "p1")["proxy_for"] == "BTC"
    assert [r["id"] for r in c.get("/api/trades?market=crypto&actor=whale").json] == ["w1"]
    assert [r["id"] for r in c.get("/api/trades?market=crypto&actor=proxy").json] == ["p1"]
    assert {r["id"] for r in c.get("/api/trades?market=stocks").json} == {"p1", "s1"}
    detail = c.get("/api/ticker/BTC?market=crypto").json
    assert {t["id"] for t in detail["trades"]} == {"p1", "w1"}
    o = c.get("/api/overview?market=crypto").json
    assert o["market"] == "crypto" and o["job_markets"]["crypto_markets"] == "crypto"
