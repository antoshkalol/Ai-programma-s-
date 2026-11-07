import json

from market_monitor.app import MarketMonitor
from market_monitor.config import load_config
from market_monitor.db import ago
from market_monitor.hass import HomeAssistant
from market_monitor.web import create_app


def test_load_config_from_options(tmp_path):
    (tmp_path / "options.json").write_text(json.dumps({
        "sec_user_agent": "Jan jan@example.com", "watchlist": ["aapl", "asml"], "summary_interval_minutes": 2,
        "x_accounts": ["@elonmusk"], "anthropic_effort": "bogus", "enable_gdelt": False,
    }))
    cfg = load_config(str(tmp_path))
    assert cfg.sec_headers_ok and cfg.watchlist == ["AAPL", "ASML"]
    assert cfg.summary_interval_minutes == 5 and cfg.x_accounts == ["elonmusk"]
    assert cfg.anthropic_effort == "medium" and cfg.enable_gdelt is False
    pub = cfg.public_dict()
    assert pub["anthropic_api_key"] is False and pub["sec_user_agent"] is True


def test_api_endpoints(tmp_path):
    cfg = load_config(str(tmp_path))
    m = MarketMonitor(cfg)
    m.db.insert_trades([
        {"id": "p1", "source": "senate", "actor_name": "Casey Example", "actor_role": "Senator",
         "actor_type": "politician", "ticker": "EXRO", "tx_type": "buy", "value_low": 50001, "value_high": 100000,
         "value_usd": 75000, "tx_date": ago(days=20)[:10], "filed_at": ago(days=1)},
        {"id": "i1", "source": "sec_form4", "actor_name": "Jane Doe", "actor_role": "CEO", "actor_type": "insider",
         "ticker": "EXRO", "tx_type": "sell", "value_usd": 5000, "filed_at": ago(days=1)},
    ])
    c = create_app(m).test_client()
    assert c.get("/").status_code == 200
    assert c.get("/static/app.js").status_code == 200
    rows = c.get("/api/trades?actor=politician").json
    assert len(rows) == 1 and rows[0]["delay_days"] == 19
    assert len(c.get("/api/trades?min_value=100000").json) == 1  # verkoop van $5k valt af
    assert c.get("/api/trades?type=sell").json[0]["id"] == "i1"
    assert c.get("/api/ticker/exro").json["ticker"] == "EXRO"
    assert c.post("/api/run/scoring").json["ok"] is True
    assert c.post("/api/run/doesnotexist").status_code == 404
    o = c.get("/api/overview").json
    assert o["stats"]["trades_24h"] == 2 and "sec_form4" in o["scheduled"]
    assert "anthropic_api_key" in o["config"] and o["config"]["anthropic_api_key"] is False


def test_hass_publish(monkeypatch, cfg, db):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    ha = HomeAssistant(cfg, db)
    calls = []

    class R:
        status_code = 200
        text = ""

    def post(url, headers=None, data=None, timeout=None):
        calls.append((url, json.loads(data)))
        assert headers["Authorization"] == "Bearer tok"
        return R()

    monkeypatch.setattr(ha.session, "post", post)
    ha.publish_new_trades([
        {"actor_name": "Jane Doe", "actor_role": "CEO", "actor_type": "insider", "tx_type": "buy", "ticker": "EXRO",
         "value_usd": 2_500_000},
        {"actor_name": "Small", "actor_type": "insider", "tx_type": "buy", "ticker": "EXRO", "value_usd": 1000},
    ])
    urls = [u for u, _ in calls]
    assert urls == ["http://supervisor/core/api/events/market_monitor_big_trade",
                    "http://supervisor/core/api/states/sensor.market_monitor_last_big_trade"]
    assert calls[0][1]["value_text"] == "$2,5 mln"
    assert calls[1][1]["state"].startswith("Jane Doe (CEO) kocht EXRO")
