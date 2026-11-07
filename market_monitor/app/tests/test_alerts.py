import json
from datetime import timedelta

from market_monitor.alerts import AlertEngine
from market_monitor.db import ago, iso, utcnow
from market_monitor.hass import HomeAssistant


class FakeHass:
    def __init__(self):
        self.pushed = []

    def push_alert(self, alert):
        self.pushed.append(alert)


class FakeSummarizer:
    def __init__(self, verdict=None, fail=False):
        self.verdict, self.fail, self.calls = verdict, fail, []

    def ask_json(self, system, text, schema, effort=None, max_tokens=None):
        self.calls.append({"text": json.loads(text), "effort": effort})
        if self.fail:
            raise RuntimeError("boom")
        return self.verdict


def _snapshot(db, coin, minutes_ago, chg_1h, volume, rank=10, price=100.0):
    db.execute(
        "INSERT INTO crypto_snapshots(coin, ts, price, volume, chg_1h, chg_24h, chg_7d, market_cap, rank)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (coin, iso(utcnow() - timedelta(minutes=minutes_ago)), price, volume, chg_1h, chg_1h, 0, 1e9, rank),
    )


def _engine(cfg, db, **kw):
    hass = FakeHass()
    return AlertEngine(cfg, db, hass, kw.get("summarizer")), hass


def test_crypto_spike_needs_volume_and_has_cooldown(cfg, db):
    _snapshot(db, "SOL", 62, 0.5, 1_000_000)
    _snapshot(db, "SOL", 1, 8.5, 1_100_000)          # +8,5% en volume +10%: alarm (drempel rang 10 = 7%)
    _snapshot(db, "ADA", 62, 0.1, 1_000_000)
    _snapshot(db, "ADA", 1, 9.0, 1_010_000)          # geen volumepiek en niet extreem: geen alarm
    _snapshot(db, "BTC", 62, 0.1, 9e9, rank=1)
    _snapshot(db, "BTC", 1, 8.1, 9e9, rank=1)        # BTC drempel 4%: 8,1% is extreem -> alarm
    eng, hass = _engine(cfg, db)
    assert eng.run() == 2
    assets = sorted(a["asset"] for a in hass.pushed)
    assert assets == ["BTC", "SOL"]
    sol = next(a for a in hass.pushed if a["asset"] == "SOL")
    assert sol["kind"] == "koersexplosie" and "+8,5%" in sol["title"]
    assert eng.run() == 0  # afkoelperiode: geen tweede melding


def test_stale_snapshots_do_not_alert(cfg, db):
    _snapshot(db, "SOL", 80, 0.5, 1e6)
    _snapshot(db, "SOL", 30, 20.0, 2e6)
    eng, hass = _engine(cfg, db)
    assert eng.check_crypto_spikes() == []


def test_sensitivity_changes_threshold(cfg, db):
    _snapshot(db, "SOL", 62, 0.5, 1e6)
    _snapshot(db, "SOL", 1, 5.5, 1.1e6)
    cfg.alert_sensitivity = "normaal"
    assert AlertEngine(cfg, db).check_crypto_spikes() == []
    cfg.alert_sensitivity = "hoog"  # drempel 7% x 0,7 = 4,9%
    assert len(AlertEngine(cfg, db).check_crypto_spikes()) == 1


def _news(db, i, domain, trust, title, tickers, sentiment=0.7, cluster="c1", market="crypto", minutes=10):
    db.execute(
        "INSERT INTO news(id, market, source, domain, title, published_at, fetched_at, trust, clickbait, credibility,"
        " cluster_id, corroboration, tickers, sentiment, is_junk) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
        (f"n{i}", market, domain, domain, title, ago(minutes=minutes), ago(minutes=1), trust, 0, trust, cluster, 3,
         json.dumps(tickers), sentiment),
    )


def test_big_news_needs_three_trusted_sources_and_catalyst(cfg, db):
    _news(db, 1, "reuters.com", 0.95, "SEC approves spot Solana ETF", ["SOL"])
    _news(db, 2, "coindesk.com", 0.75, "Solana jumps as SEC approves ETF", ["SOL"])
    eng, hass = _engine(cfg, db)
    assert eng.check_news("crypto") == []  # pas 2 betrouwbare bronnen
    _news(db, 3, "theblock.co", 0.75, "First Solana ETF approved", ["SOL"])
    cands = eng.check_news("crypto")
    assert len(cands) == 1 and cands[0]["asset"] == "SOL" and cands[0]["kind"] == "groot_nieuws"
    _news(db, 4, "a.com", 0.95, "Solana rallies strongly", ["SOL"], cluster="c2")
    _news(db, 5, "b.com", 0.95, "Solana rallies strongly today", ["SOL"], cluster="c2")
    _news(db, 6, "c.com", 0.95, "Solana rallies strongly again", ["SOL"], cluster="c2")
    assert len(eng.check_news("crypto")) == 1  # cluster c2 heeft geen concrete aanleiding


def test_insider_cluster_and_big_politician(cfg, db):
    db.insert_trades([
        {"id": f"i{k}", "source": "sec_form4", "actor_name": n, "actor_role": "Director", "actor_type": "insider",
         "ticker": "EXRO", "tx_type": "buy", "value_usd": 400_000, "filed_at": ago(days=2)}
        for k, n in enumerate(["A", "B", "C"])
    ] + [
        {"id": "p1", "source": "house", "actor_name": "Alex Example", "actor_role": "Afgevaardigde",
         "actor_type": "politician", "ticker": "SMPL", "tx_type": "buy", "value_low": 1_000_001,
         "value_high": 5_000_000, "value_usd": 3_000_000, "tx_date": ago(days=20)[:10], "filed_at": ago(hours=3)},
    ])
    eng, hass = _engine(cfg, db)
    assert eng.run() == 2
    kinds = {a["kind"]: a for a in hass.pushed}
    assert "3 insiders" in kinds["insider_cluster"]["title"]
    assert "20 dagen voor de melding" in kinds["politicus_groot"]["message"]


def test_score_jump(cfg, db):
    db.execute("INSERT INTO scores_latest(market, ticker, computed_at, score, prob_up, reasons) VALUES "
               "('stocks','EXRO',?,82,0.7,'[]')", (iso(utcnow()),))
    db.set_state("score_snap_stocks", {"ts": ago(minutes=70), "scores": {"EXRO": 60}})
    eng, _ = _engine(cfg, db)
    cands = eng.check_score_jumps("stocks")
    assert len(cands) == 1 and "82" in cands[0]["title"]
    assert db.get_state("score_snap_stocks")["scores"]["EXRO"] == 82  # snapshot ververst


def test_daily_cap(cfg, db):
    cfg.max_alerts_per_day = 1
    eng, hass = _engine(cfg, db)
    base = {"market": "crypto", "kind": "koersexplosie", "title": "x", "message": "", "reasons": []}
    assert eng.emit({**base, "asset": "SOL"}) == 1
    assert eng.emit({**base, "asset": "ADA"}) == 0
    statuses = [r["status"] for r in db.query("SELECT status FROM alerts ORDER BY id")]
    assert statuses == ["verzonden", "onderdrukt"] and len(hass.pushed) == 1


def test_ai_can_veto_or_improve_alert(cfg, db):
    cfg.anthropic_api_key = "sk-test"
    base = {"market": "crypto", "kind": "groot_nieuws", "message": "m", "reasons": ["r"]}
    veto = FakeSummarizer({"send": False, "title": "", "explanation": "Alleen een gerucht.", "risk": "onbevestigd"})
    eng, hass = _engine(cfg, db, summarizer=veto)
    assert eng.emit({**base, "asset": "SOL", "title": "SOL nieuws"}) == 0
    row = db.query_one("SELECT * FROM alerts WHERE asset='SOL'")
    assert row["status"] == "onderdrukt" and "gerucht" in row["ai_note"] and not hass.pushed
    assert veto.calls[0]["effort"] == "low" and veto.calls[0]["text"]["signaal"]["asset"] == "SOL"

    ok = FakeSummarizer({"send": True, "title": "ETF goedgekeurd: ADA stijgt", "explanation": "Concreet nieuws.",
                         "risk": "Stijging deels gebeurd."})
    eng2, hass2 = _engine(cfg, db, summarizer=ok)
    assert eng2.emit({**base, "asset": "ADA", "title": "ADA nieuws"}) == 1
    assert hass2.pushed[0]["title"] == "ETF goedgekeurd: ADA stijgt"

    broken = FakeSummarizer(fail=True)  # AI-fout: melding gaat toch door
    eng3, hass3 = _engine(cfg, db, summarizer=broken)
    assert eng3.emit({**base, "asset": "DOT", "title": "DOT nieuws"}) == 1


def test_hass_push_alert_and_hourly_notification(monkeypatch, cfg, db):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    cfg.notify_service = "mobile_app_test"
    ha = HomeAssistant(cfg, db)
    calls = []

    class R:
        status_code = 200
        text = ""

    monkeypatch.setattr(ha.session, "post", lambda url, headers=None, data=None, timeout=None:
                        calls.append((url, json.loads(data))) or R())
    alert = {"id": 7, "market": "crypto", "asset": "SOL", "kind": "koersexplosie", "title": "SOL +9%",
             "message": "Snel omhoog", "ai_note": "Let op hype", "strength": 1.3, "created_at": iso(utcnow())}
    ha.push_alert(alert)
    urls = [u.rsplit("/api", 1)[1] for u, _ in calls]
    assert urls == ["/services/notify/mobile_app_test", "/services/persistent_notification/create",
                    "/events/market_monitor_alert", "/states/sensor.market_monitor_alert"]
    push = calls[0][1]
    assert push["title"] == "Noodmelding crypto: SOL +9%"
    assert push["data"]["push"]["sound"]["critical"] == 1 and push["data"]["priority"] == "high"
    assert "AI: Let op hype" in push["message"]

    calls.clear()
    cfg.alert_critical = False
    ha.push_alert({**alert, "id": 8})
    assert calls[0][1]["data"]["push"] == {"interruption-level": "time-sensitive"}

    # Uurlijkse samenvatting: sensoren per markt + één gewoon pushbericht voor beide markten
    calls.clear()
    for market, head in (("stocks", "Aandelen rustig"), ("crypto", "Bitcoin stijgt")):
        db.execute("INSERT INTO summaries(market, period_start, period_end, created_at, method, content)"
                   " VALUES (?,?,?,?,?,?)", (market, ago(hours=1), iso(utcnow()), iso(utcnow()), "automatisch",
                                             json.dumps({"headline": head, "key_points": ["a"]})))
    ha.publish_summaries(["stocks", "crypto"])
    notify = [d for u, d in calls if u.endswith("/services/notify/mobile_app_test")]
    assert len(notify) == 1
    assert notify[0]["message"].splitlines() == ["Aandelen: Aandelen rustig", "Crypto: Bitcoin stijgt"]
    data = notify[0]["data"]
    # Uurbericht: wel popup (hoge prioriteit), maar geen kritieke melding en vervangt het vorige uurbericht
    assert data["tag"] == "market_monitor_update" and data["push"] == {"interruption-level": "active"}
    assert data["channel"] == "Market Monitor"
    assert any(u.endswith("sensor.market_monitor_crypto_summary") for u, _ in calls)
