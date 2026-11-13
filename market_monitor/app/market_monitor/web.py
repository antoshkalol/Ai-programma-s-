"""Webserver: dashboard + JSON-API. Werkt achter Home Assistant Ingress (alle paden zijn relatief).

Alle data-endpoints nemen ?market=stocks of ?market=crypto.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory

from . import __version__
from .config import MARKET_LABELS, MARKETS
from .db import ago, iso, utcnow
from .markets import crypto_proxies
from .summary import latest_summary, trades_sql
from .trading import settings as tsettings
from .trading.brokers import BitvavoBroker, BrokerError, Trading212Broker
from .trading.engine import CONFIRM_TEXT

STATIC = Path(__file__).parent / "static"


def _int(name: str, default: int, lo: int = 0, hi: int = 1000) -> int:
    try:
        return max(lo, min(hi, int(request.args.get(name, default))))
    except (TypeError, ValueError):
        return default


def _market() -> str:
    m = request.args.get("market", "stocks")
    return m if m in MARKETS else "stocks"


def _delay_days(tx_date: str | None, filed_at: str | None) -> int | None:
    if not tx_date or not filed_at:
        return None
    try:
        a = datetime.strptime(tx_date[:10], "%Y-%m-%d")
        b = datetime.strptime(filed_at[:10], "%Y-%m-%d")
        return max(0, (b - a).days)
    except ValueError:
        return None


def _decorate_trades(rows: list[dict]) -> list[dict]:
    proxies = crypto_proxies()
    for r in rows:
        r["delay_days"] = (_delay_days(r.get("tx_date"), r.get("filed_at"))
                           if r["actor_type"] in ("insider", "politician") else None)
        if r["market"] == "stocks" and r.get("ticker") in proxies:
            r["proxy_for"] = proxies[r["ticker"]] or "crypto"
    return rows


def create_app(monitor) -> Flask:
    app = Flask(__name__, static_folder=None)
    db, cfg = monitor.db, monitor.cfg

    @app.after_request
    def no_cache(resp):
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/static/<path:name>")
    def static_files(name):
        return send_from_directory(STATIC, name)

    @app.get("/api/overview")
    def overview():
        market = _market()
        since = ago(hours=24)
        where, args = trades_sql(market)
        stats = {
            "trades_24h": db.query_one(f"SELECT COUNT(*) n FROM trades WHERE {where} AND created_at >= ?",
                                       (*args, since))["n"],
            "news_24h": db.query_one("SELECT COUNT(*) n FROM news WHERE market=? AND is_junk=0 AND published_at >= ?",
                                     (market, since))["n"],
            "filtered_24h": db.query_one("SELECT COUNT(*) n FROM news WHERE market=? AND is_junk=1 AND published_at >= ?",
                                         (market, since))["n"],
        }
        jobs = db.query("SELECT * FROM job_status ORDER BY name")
        return jsonify(
            {
                "version": __version__,
                "now": iso(utcnow()),
                "market": market,
                "markets": [{"key": m, "label": MARKET_LABELS[m]} for m in cfg.markets],
                "summary": latest_summary(db, market),
                "stats": stats,
                "jobs": jobs,
                "scheduled": list(monitor.scheduler.jobs),
                "job_markets": monitor.job_markets,
                "config": cfg.public_dict(),
                "calibration": db.get_state(f"calibration_{market}"),
                "recommendation": db.get_state(f"recommendation_{market}"),
                "notify": {"service": cfg.notify_service or "automatisch", "in_home_assistant": monitor.hass.enabled},
            }
        )

    @app.get("/api/trades")
    def trades():
        market = _market()
        actor = request.args.get("actor", "all")
        tx = request.args.get("type", "all")
        min_value = _int("min_value", 0, 0, 10**12)
        days = _int("days", 30, 1, 365)
        limit = _int("limit", 150, 1, 1000)
        where, args = trades_sql(market)
        sql = f"SELECT * FROM trades WHERE {where} AND COALESCE(filed_at, created_at) >= ?"
        args = [*args, ago(days=days)]
        if actor in ("politician", "insider", "company", "whale"):
            sql += " AND actor_type=?"
            args.append(actor)
        elif actor == "proxy":
            sql += " AND market='stocks'"
        if tx in ("buy", "sell"):
            sql += " AND tx_type=?"
            args.append(tx)
        else:
            sql += " AND (tx_type IN ('buy','sell') OR actor_type IN ('politician','whale'))"
        if min_value:
            sql += " AND COALESCE(value_high, value_usd, 0) >= ?"
            args.append(min_value)
        sql += " ORDER BY COALESCE(filed_at, created_at) DESC, created_at DESC LIMIT ?"
        args.append(limit)
        return jsonify(_decorate_trades(db.query(sql, args)))

    @app.get("/api/news")
    def news():
        market = _market()
        junk = request.args.get("junk", "0") == "1"
        ticker = (request.args.get("ticker") or "").upper()
        limit = _int("limit", 120, 1, 500)
        sql = "SELECT * FROM news WHERE market=? AND is_junk=? AND published_at >= ?"
        args: list = [market, 1 if junk else 0, ago(hours=48)]
        if ticker:
            sql += " AND tickers LIKE ?"
            args.append(f'%"{ticker}"%')
        sql += " ORDER BY published_at DESC LIMIT ?"
        args.append(limit)
        rows = db.query(sql, args)
        for r in rows:
            r["tickers"] = json.loads(r["tickers"] or "[]")
        return jsonify(rows)

    @app.get("/api/summaries")
    def summaries():
        rows = db.query("SELECT * FROM summaries WHERE market=? ORDER BY id DESC LIMIT ?",
                        (_market(), _int("limit", 12, 1, 100)))
        for r in rows:
            r["content"] = json.loads(r["content"])
        return jsonify(rows)

    @app.get("/api/scores")
    def scores():
        market = _market()
        rows = db.query(
            "SELECT s.*, m.name, m.prev_close, m.currency, m.chg_24h, m.chg_7d, m.rank FROM scores_latest s "
            "LEFT JOIN price_meta m ON m.market = s.market AND m.ticker = s.ticker WHERE s.market=?"
            " ORDER BY s.score DESC LIMIT ?",
            (market, _int("limit", 25, 1, 200)),
        )
        for r in rows:
            r["components"] = json.loads(r["components"] or "{}")
            r["reasons"] = json.loads(r["reasons"] or "[]")
            if r.get("chg_24h") is not None:
                r["change_pct"] = r["chg_24h"]
            else:
                r["change_pct"] = (
                    (r["price"] / r["prev_close"] - 1) * 100 if r.get("price") and r.get("prev_close") else None
                )
        return jsonify(rows)

    @app.get("/api/ticker/<symbol>")
    def ticker(symbol: str):
        market = _market()
        sym = symbol.upper()[:12]
        prices = db.query("SELECT date, close FROM prices WHERE market=? AND ticker=? ORDER BY date DESC LIMIT 130",
                          (market, sym))[::-1]
        score = db.query_one("SELECT * FROM scores_latest WHERE market=? AND ticker=?", (market, sym))
        if score:
            score["components"] = json.loads(score["components"] or "{}")
            score["reasons"] = json.loads(score["reasons"] or "[]")
        if market == "crypto":
            proxies = [k for k, v in crypto_proxies().items() if v == sym]
            ph = ",".join("?" * len(proxies)) or "''"
            trades_rows = db.query(
                f"SELECT * FROM trades WHERE ((market='crypto' AND ticker=?) OR (market='stocks' AND ticker IN ({ph})))"
                " AND COALESCE(filed_at, created_at) >= ? ORDER BY COALESCE(filed_at, created_at) DESC LIMIT 50",
                (sym, *proxies, ago(days=90)),
            )
        else:
            trades_rows = db.query(
                "SELECT * FROM trades WHERE market='stocks' AND ticker=? AND COALESCE(filed_at, created_at) >= ? "
                "ORDER BY COALESCE(filed_at, created_at) DESC LIMIT 50",
                (sym, ago(days=90)),
            )
        news_rows = db.query(
            "SELECT * FROM news WHERE market=? AND is_junk=0 AND tickers LIKE ? AND published_at >= ? "
            "ORDER BY published_at DESC LIMIT 30",
            (market, f'%"{sym}"%', ago(days=7)),
        )
        for n in news_rows:
            n["tickers"] = json.loads(n["tickers"] or "[]")
        meta = db.query_one("SELECT * FROM price_meta WHERE market=? AND ticker=?", (market, sym))
        return jsonify({"ticker": sym, "market": market, "prices": prices, "meta": meta, "score": score,
                        "trades": _decorate_trades(trades_rows), "news": news_rows})

    @app.get("/api/alerts")
    def alerts():
        hours = _int("hours", 24, 1, 24 * 30)
        rows = db.query("SELECT * FROM alerts WHERE created_at >= ? ORDER BY id DESC LIMIT 50", (ago(hours=hours),))
        for r in rows:
            r["reasons"] = json.loads(r["reasons"] or "[]")
        return jsonify(rows)

    @app.post("/api/alerts/<int:alert_id>/ack")
    def ack_alert(alert_id: int):
        if not db.execute("UPDATE alerts SET acknowledged=1 WHERE id=?", (alert_id,)):
            abort(404)
        return jsonify({"ok": True})

    @app.get("/api/recommendation")
    def recommendation():
        market = _market()
        rec = monitor.recommender.current(market)
        if rec is None and db.query_one("SELECT 1 FROM scores_latest WHERE market=?", (market,)):
            # Nog niet berekend sinds de start: nu doen
            monitor.recommender.update(market)
            rec = monitor.recommender.current(market)
        comment = None
        s = latest_summary(db, market)
        if s and rec:
            ref = s["content"].get("recommendation_ref") or {}
            if ref.get("ticker") == rec["ticker"] and ref.get("since") == rec.get("since"):
                comment = s["content"].get("recommendation_comment")
        fallback = None
        if rec is None:
            # Geen technische analyse mogelijk: toon dan het beste kandidaat volgens de koopscore
            top = db.query_one("SELECT s.*, m.name, m.currency, m.chg_24h FROM scores_latest s LEFT JOIN price_meta m"
                               " ON m.market=s.market AND m.ticker=s.ticker WHERE s.market=? ORDER BY s.score DESC"
                               " LIMIT 1", (market,))
            if top:
                top["reasons"] = json.loads(top["reasons"] or "[]")
                job = db.query_one("SELECT last_error FROM job_status WHERE name=?",
                                   ("prices_stocks" if market == "stocks" else "crypto_history",))
                n = db.query_one("SELECT COUNT(*) n FROM prices WHERE market=? AND ticker=?",
                                 (market, top["ticker"]))["n"]
                fallback = {"top": top, "price_days": n, "price_error": (job or {}).get("last_error"),
                            "price_sources": db.get_state("price_sources") if market == "stocks" else None}
        return jsonify({"market": market, "label": MARKET_LABELS[market], "recommendation": rec,
                        "fallback": fallback, "ai_comment": comment, "comment_method": s["method"] if s else None,
                        "overview": s["content"].get("overview") if s else None,
                        "overview_at": s["created_at"] if s else None,
                        "history": monitor.recommender.history(market)})

    @app.post("/api/summary/now")
    def summary_now():
        """Maakt direct een samenvatting van het afgelopen uur voor één markt (zonder pushbericht)."""
        market = _market()
        end = utcnow()
        start = end - timedelta(minutes=cfg.summary_interval_minutes)
        content = monitor.summarizer.run_market(market, iso(start), iso(end))
        return jsonify({"ok": True, "market": market, "content": content})

    @app.post("/api/notify/test")
    def notify_test():
        return jsonify(monitor.hass.send_test())

    # ------------------------------------------------------------------ handelen
    trading = monitor.trading

    def _json_body() -> dict:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    @app.get("/api/trading")
    def trading_overview():
        runs = db.query("SELECT id FROM trading_runs ORDER BY id DESC LIMIT 40")
        summaries = [trading.run_summary(trading.get_run(r["id"])) for r in runs]
        current = trading.settings()
        return jsonify({
            "settings": current,
            "schema": tsettings.schema(),
            "runs": summaries,
            "live_checks": trading.live_checks(current),
            "allow_live": cfg.allow_live_trading,
            "ai_available": cfg.llm_enabled,
            "keys": {"bitvavo": bool(cfg.bitvavo_api_key and cfg.bitvavo_api_secret),
                     "trading212": bool(cfg.trading212_api_key)},
            "confirm_text": CONFIRM_TEXT,
            "markets": cfg.markets,
        })

    @app.post("/api/trading/settings")
    def trading_save_settings():
        new, errors = trading.save_settings(_json_body())
        return jsonify({"ok": not errors, "settings": new, "errors": errors}), (200 if not errors else 400)

    @app.post("/api/trading/settings/reset")
    def trading_reset_settings():
        db.set_state(tsettings.STATE_KEY, dict(tsettings.DEFAULTS))
        return jsonify({"ok": True, "settings": trading.settings()})

    @app.post("/api/trading/settings/from/<int:run_id>")
    def trading_settings_from_run(run_id: int):
        run = trading.get_run(run_id)
        if not run:
            abort(404)
        new, errors = trading.save_settings(run["settings"])
        return jsonify({"ok": not errors, "settings": new, "errors": errors})

    @app.post("/api/trading/runs")
    def trading_start():
        body = _json_body()
        hours = body.get("hours")
        try:
            hours = float(hours) if hours not in (None, "", 0, "0") else None
            run = trading.start_run(body.get("mode", "paper"), str(body.get("name") or "").strip(), hours,
                                    str(body.get("confirm") or ""))
        except (TypeError, ValueError) as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        return jsonify({"ok": True, "run": trading.run_summary(run)})

    @app.get("/api/trading/runs/<int:run_id>")
    def trading_run(run_id: int):
        d = trading.detail(run_id)
        if not d:
            abort(404)
        return jsonify(d)

    @app.post("/api/trading/runs/<int:run_id>/stop")
    def trading_stop(run_id: int):
        body = _json_body()
        run = trading.stop_run(run_id, close_positions=bool(body.get("close", True)),
                               reason=str(body.get("reason") or "Handmatig gestopt")[:200])
        if not run:
            abort(404)
        return jsonify({"ok": True, "run": trading.run_summary(run)})

    @app.post("/api/trading/runs/<int:run_id>/delete")
    def trading_delete(run_id: int):
        run = trading.get_run(run_id)
        if not run:
            abort(404)
        if run["status"] == "running":
            return jsonify({"ok": False, "message": "Stop de run eerst"}), 400
        for table in ("trading_positions", "trading_fills", "trading_decisions", "trading_equity"):
            db.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
        db.execute("DELETE FROM trading_runs WHERE id=?", (run_id,))
        return jsonify({"ok": True})

    @app.post("/api/trading/check")
    def trading_check():
        """Verbinding met de brokers testen: alleen saldo lezen, nooit een order."""
        current = trading.settings()
        out = []
        if cfg.bitvavo_api_key and cfg.bitvavo_api_secret:
            try:
                b = BitvavoBroker(cfg.bitvavo_api_key, cfg.bitvavo_api_secret, current, dry_run=True)
                out.append({"broker": "Bitvavo", "ok": True,
                            "message": f"Verbonden. Vrij saldo: €{b.free_cash_eur():,.2f}"})
            except BrokerError as exc:
                out.append({"broker": "Bitvavo", "ok": False, "message": str(exc)})
        else:
            out.append({"broker": "Bitvavo", "ok": False, "message": "Geen API-sleutel ingevuld"})
        if cfg.trading212_api_key:
            try:
                b = Trading212Broker(cfg.trading212_api_key, cfg.trading212_api_secret, current, dry_run=True, db=db)
                cash = b.free_cash_eur()
                env = current["trading212_env"]
                out.append({"broker": f"Trading 212 ({env})", "ok": True,
                            "message": "Verbonden." + (f" Vrij saldo: €{cash:,.2f}" if cash is not None else "")})
            except BrokerError as exc:
                out.append({"broker": "Trading 212", "ok": False, "message": str(exc)})
        else:
            out.append({"broker": "Trading 212", "ok": False, "message": "Geen API-sleutel ingevuld"})
        return jsonify({"results": out})

    @app.post("/api/run/<job>")
    def run_job(job: str):
        if not monitor.scheduler.trigger(job):
            abort(404)
        return jsonify({"ok": True, "job": job})

    return app
