import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from market_monitor.db import iso, utcnow
from market_monitor.filtering import NewsIngestor, SourceTrust, clickbait_score, credibility
from market_monitor.scoring import ScoringEngine, indicators, insider_component, timing_of
from market_monitor.sentiment import headline_sentiment
from market_monitor.sources import news as news_src
from market_monitor.summary import Summarizer
from market_monitor.tickers import TickerExtractor, clean_company_name


def _now(minutes=0):
    return iso(utcnow() - timedelta(minutes=minutes))


# --------------------------------------------------------------------------- filter
def test_clickbait_detection():
    assert clickbait_score("This PENNY STOCK Could Make You A MILLIONAIRE Before It's Too Late!") >= 0.7
    assert clickbait_score("Fed holds rates steady, signals patience") < 0.2
    assert clickbait_score("Is Example Robotics a buy right now?") >= 0.15


def test_trust_and_credibility(cfg):
    cfg.trusted_domains = ["mijnbron.nl"]
    cfg.blocked_domains = ["spam.example"]
    t = SourceTrust(cfg)
    assert t.trust("reuters.com") == 0.95
    assert t.trust("uk.reuters.com") == 0.95
    assert t.trust("mijnbron.nl") == 0.9
    assert t.trust("onbekend.example") == 0.35
    assert t.trust("prnewswire.com") == 0.5
    assert t.trust("x.com", "elonmusk", {"elonmusk"}) == 0.7
    assert t.is_blocked("www.spam.example".removeprefix("www."))
    # Bevestiging door meerdere bronnen tilt een onbekende bron over de drempel
    assert credibility(0.35, 0.0, 1) < 0.45 <= credibility(0.35, 0.0, 3)


def test_ingest_clusters_filters_and_extracts(cfg, db, fixture_text):
    items = news_src.parse_rss(fixture_text("google_news.xml"), "Google News", google_news=True)
    for it in items:
        it["published_at"] = _now(5)
    ing = NewsIngestor(cfg, db, {"stocks": TickerExtractor(db, cfg.stock_watchlist)})
    assert ing(items) == 3
    assert ing(items) == 0  # ontdubbeld
    rows = {r["domain"]: r for r in db.query("SELECT * FROM news")}
    reuters, cnbc, hype = rows["reuters.com"], rows["cnbc.com"], rows["stockhypedaily.example"]
    assert reuters["cluster_id"] == cnbc["cluster_id"]  # zelfde verhaal
    assert reuters["corroboration"] == 2
    assert reuters["is_junk"] == 0 and reuters["credibility"] > 0.9
    assert hype["is_junk"] == 1 and hype["junk_reason"] == "Clickbait/hype"
    assert json.loads(reuters["tickers"]) == []  # 'Example Robotics' is (nog) geen bekende naam
    assert reuters["sentiment"] > 0.3
    # Verschillende bedrijven met bijna dezelfde kop worden niet samengevoegd
    other = [
        {"id": "n1", "source": "Reuters", "domain": "reuters.com", "title": "Nvidia earnings beat estimates",
         "url": "https://r/1", "published_at": _now(2)},
        {"id": "n2", "source": "CNBC", "domain": "cnbc.com", "title": "Apple earnings beat estimates",
         "url": "https://c/2", "published_at": _now(2)},
    ]
    ing(other)
    c1, c2 = (db.query_one("SELECT cluster_id FROM news WHERE id=?", (i,))["cluster_id"] for i in ("stocks:n1", "stocks:n2"))
    assert c1 != c2


def test_ticker_extraction_uses_trade_company_names(db, cfg):
    db.insert_trades([{"id": "t1", "source": "sec_form4", "actor_name": "X", "actor_type": "insider",
                       "ticker": "EXRO", "company": "EXAMPLE ROBOTICS CORP", "tx_type": "buy", "filed_at": _now()}])
    ex = TickerExtractor(db, cfg.stock_watchlist)
    assert ex.extract("Example Robotics shares jump") == ["EXRO"]
    assert ex.extract("Apple and $TSLA rally; (NASDAQ: SMPL) too") == ["TSLA", "SMPL", "AAPL"]
    assert ex.extract("THE CEO SAID IT") == []
    assert clean_company_name("NVIDIA CORP /DE/") == "Nvidia"


def test_sentiment_lexicon():
    assert headline_sentiment("Example beats estimates and raises guidance") > 0.5
    assert headline_sentiment("Shares plunge after SEC probe and lawsuit") < -0.5
    assert headline_sentiment("Company did not miss estimates") > 0
    assert headline_sentiment("Aandeel daalt na winstwaarschuwing") < -0.4


# --------------------------------------------------------------------------- score
def test_indicators_and_timing():
    up = [100 + i * 0.5 for i in range(80)]
    pull = up[:-5] + [up[-6] - 1.0 * k for k in range(1, 6)]  # terugval van ~4% in een stijgende trend
    ind = indicators(pull)
    assert ind["sma50"] < ind["last"]
    label, score = timing_of(ind)
    assert label.startswith("Gunstig") and score > 0
    down = [200 - i for i in range(80)]
    assert timing_of(indicators(down))[0].startswith("Dalende")
    assert timing_of({})[0] == "Geen koersdata"


def test_insider_cluster_buy_scores_higher():
    one = [{"tx_type": "buy", "value_usd": 500_000, "planned": 0, "actor_name": "A", "actor_role": "Director"}]
    three = one + [
        {"tx_type": "buy", "value_usd": 500_000, "planned": 0, "actor_name": "B", "actor_role": "CEO"},
        {"tx_type": "buy", "value_usd": 500_000, "planned": 0, "actor_name": "C", "actor_role": "CFO"},
    ]
    s1, _ = insider_component(one)
    s3, reasons = insider_component(three)
    assert 0 < s1 < s3 <= 1
    assert "3 insider(s)" in reasons[0] and "topbestuurder" in reasons[0]


def _seed_prices(db, ticker, closes):
    start = utcnow() - timedelta(days=len(closes))
    db.executemany("INSERT INTO prices(ticker, date, close) VALUES (?,?,?)",
                   [(ticker, (start + timedelta(days=i)).strftime("%Y-%m-%d"), c) for i, c in enumerate(closes)])


def test_scoring_engine_ranks_signals(cfg, db):
    db.insert_trades([
        {"id": f"b{i}", "source": "sec_form4", "actor_name": n, "actor_role": "CEO", "actor_type": "insider",
         "ticker": "EXRO", "company": "Example Robotics", "tx_type": "buy", "value_usd": 2_000_000,
         "filed_at": _now(60), "tx_date": _now(3000)[:10]} for i, n in enumerate(["A", "B"])
    ] + [
        {"id": "s1", "source": "sec_form4", "actor_name": "Z", "actor_role": "Director", "actor_type": "insider",
         "ticker": "SMPL", "company": "Sample Software", "tx_type": "sell", "value_usd": 30_000_000,
         "filed_at": _now(60)},
    ])
    _seed_prices(db, "EXRO", [50 + i * 0.3 for i in range(90)])
    _seed_prices(db, "SMPL", [120 - i * 0.6 for i in range(90)])
    eng = ScoringEngine(cfg, db)
    assert eng.run() == 2
    rows = {r["ticker"]: r for r in db.query("SELECT * FROM scores_latest")}
    assert rows["EXRO"]["score"] > 60 > rows["SMPL"]["score"]
    assert 0.25 <= rows["SMPL"]["prob_up"] < rows["EXRO"]["prob_up"] <= 0.80
    assert rows["EXRO"]["prob_source"] == "model"
    assert db.query_one("SELECT COUNT(*) n FROM score_history")["n"] == 2


def test_probability_calibration_overrides_model(cfg, db):
    eng = ScoringEngine(cfg, db)
    p_model, src = eng.probability(75, None)
    assert src == "model"
    calib = {"70-101": {"n": 200, "up": 80}}  # in de praktijk maar 40% raak
    p_cal, src2 = eng.probability(75, calib)
    assert src2 == "gekalibreerd" and p_cal < p_model


# --------------------------------------------------------------------------- samenvatting
def _seed_news(db, title, domain, cred, tickers, minutes=5, sentiment=0.5, kind="news"):
    db.execute(
        "INSERT INTO news(id, source, domain, kind, title, url, published_at, fetched_at, trust, clickbait,"
        " credibility, cluster_id, corroboration, tickers, sentiment, is_junk) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
        (title[:10], domain, domain, kind, title, "https://" + domain, _now(minutes), _now(), cred, 0, cred,
         title[:10], 1, json.dumps(tickers), sentiment),
    )


def test_extractive_summary(cfg, db):
    _seed_news(db, "Example Robotics jumps on record quarter", "reuters.com", 0.95, ["EXRO"])
    _seed_news(db, "Old news from yesterday", "reuters.com", 0.95, [], minutes=600)
    db.insert_trades([{"id": "p1", "source": "senate", "actor_name": "Casey Example", "actor_role": "Senator",
                       "actor_type": "politician", "ticker": "EXRO", "tx_type": "buy", "value_low": 50001,
                       "value_high": 100000, "value_usd": 75000, "filed_at": _now(10)}])
    s = Summarizer(cfg, db)
    assert s.run() == 2  # aandelen + crypto
    row = db.query_one("SELECT * FROM summaries WHERE market='stocks'")
    c = json.loads(row["content"])
    assert row["method"] == "automatisch"
    assert c["headline"] == "Example Robotics jumps on record quarter"
    assert c["stats"]["news"] == 1
    assert any("Casey Example" in t and "$50k-$100k" in t for t in c["notable_trades"])
    assert any("45 dagen" in t for t in c["notable_trades"])


class _FakeMessages:
    def __init__(self, payload, stop="end_turn"):
        self.payload, self.stop, self.calls = payload, stop, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(stop_reason=self.stop,
                               content=[SimpleNamespace(type="text", text=json.dumps(self.payload))])


def _fake_client(payload, stop="end_turn"):
    msgs = _FakeMessages(payload, stop)
    return SimpleNamespace(messages=msgs, beta=SimpleNamespace(messages=msgs)), msgs


AI_RESULT = {
    "headline": "Robotica in trek", "market_mood": "positief", "key_points": ["Example Robotics stijgt"],
    "notable_trades": [], "tickers": [{"ticker": "$exro", "sentiment": 1.7, "reason": "sterke cijfers"},
                                      {"ticker": "bad ticker!", "sentiment": 0.1, "reason": "x"}],
    "watch_out": [], "unreliable_items": [1, 99], "buy_view": "EXRO oogt interessant, maar let op de waardering.",
}


@pytest.mark.parametrize("model,expect_beta", [("claude-opus-5-5", True), ("claude-haiku-5-5", False)])
def test_ai_summary_request_shape_and_side_effects(cfg, db, model, expect_beta):
    cfg.anthropic_api_key = "sk-test"
    cfg.anthropic_model = model
    _seed_news(db, "Example Robotics jumps on record quarter", "reuters.com", 0.95, ["EXRO"])
    _seed_news(db, "Rumour: EXRO to be bought for 10x", "rumours.example", 0.5, ["EXRO"])
    ing = NewsIngestor(cfg, db, {"stocks": TickerExtractor(db, cfg.stock_watchlist)})
    s = Summarizer(cfg, db, ing)
    client, msgs = _fake_client(AI_RESULT)
    s._client = client
    assert s.run() == 2
    assert len(msgs.calls) == 2
    call = msgs.calls[0]
    assert call["model"] == model
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["effort"] == "medium"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert ("fallbacks" in call) == expect_beta
    if expect_beta:
        assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    row = db.query_one("SELECT * FROM summaries WHERE market='stocks'")
    c = json.loads(row["content"])
    assert row["method"] == "ai" and c["headline"] == "Robotica in trek" and "unreliable_items" not in c
    assert c["filtered_by_ai"] == 1
    sent = db.query("SELECT * FROM ticker_sentiment WHERE market='stocks'")
    assert [(r["ticker"], r["sentiment"]) for r in sent] == [("EXRO", 1.0)]
    junk = db.query("SELECT title, junk_reason FROM news WHERE is_junk=1")
    assert junk == [{"title": "Rumour: EXRO to be bought for 10x", "junk_reason": "AI: hype of gerucht"}]


def test_ai_failure_falls_back_to_extractive(cfg, db):
    cfg.anthropic_api_key = "sk-test"
    s = Summarizer(cfg, db)
    client, _ = _fake_client(AI_RESULT, stop="refusal")
    s._client = client
    s.run()
    assert {r["method"] for r in db.query("SELECT method FROM summaries")} == {"automatisch"}
