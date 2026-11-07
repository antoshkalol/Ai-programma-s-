"""Webserver: dashboard + JSON-API. Werkt achter Home Assistant Ingress (alle paden zijn relatief)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory

from . import __version__
from .db import ago, iso, utcnow

STATIC = Path(__file__).parent / "static"


def _int(name: str, default: int, lo: int = 0, hi: int = 1000) -> int:
    try:
        return max(lo, min(hi, int(request.args.get(name, default))))
    except (TypeError, ValueError):
        return default


def _delay_days(tx_date: str | None, filed_at: str | None) -> int | None:
    if not tx_date or not filed_at:
        return None
    try:
        a = datetime.strptime(tx_date[:10], "%Y-%m-%d")
        b = datetime.strptime(filed_at[:10], "%Y-%m-%d")
        return max(0, (b - a).days)
    except ValueError:
        return None


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
        from .summary import latest_summary

        since = ago(hours=24)
        stats = {
            "trades_24h": db.query_one("SELECT COUNT(*) n FROM trades WHERE created_at >= ?", (since,))["n"],
            "news_24h": db.query_one("SELECT COUNT(*) n FROM news WHERE is_junk=0 AND published_at >= ?",
                                     (since,))["n"],
            "filtered_24h": db.query_one("SELECT COUNT(*) n FROM news WHERE is_junk=1 AND published_at >= ?",
                                         (since,))["n"],
        }
        jobs = db.query("SELECT * FROM job_status ORDER BY name")
        return jsonify(
            {
                "version": __version__,
                "now": iso(utcnow()),
                "summary": latest_summary(db),
                "stats": stats,
                "jobs": jobs,
                "scheduled": list(monitor.scheduler.jobs),
                "config": cfg.public_dict(),
                "calibration": db.get_state("calibration"),
            }
        )

    @app.get("/api/trades")
    def trades():
        actor = request.args.get("actor", "all")
        tx = request.args.get("type", "all")
        min_value = _int("min_value", 0, 0, 10**12)
        days = _int("days", 30, 1, 365)
        limit = _int("limit", 150, 1, 1000)
        sql = "SELECT * FROM trades WHERE COALESCE(filed_at, created_at) >= ?"
        args: list = [ago(days=days)]
        if actor in ("politician", "insider"):
            sql += " AND actor_type=?"
            args.append(actor)
        if tx in ("buy", "sell"):
            sql += " AND tx_type=?"
            args.append(tx)
        else:
            sql += " AND (tx_type IN ('buy','sell') OR actor_type='politician')"
        if min_value:
            sql += " AND COALESCE(value_high, value_usd, 0) >= ?"
            args.append(min_value)
        sql += " ORDER BY COALESCE(filed_at, created_at) DESC, created_at DESC LIMIT ?"
        args.append(limit)
        rows = db.query(sql, args)
        for r in rows:
            r["delay_days"] = _delay_days(r.get("tx_date"), r.get("filed_at"))
        return jsonify(rows)

    @app.get("/api/news")
    def news():
        junk = request.args.get("junk", "0") == "1"
        ticker = (request.args.get("ticker") or "").upper()
        limit = _int("limit", 120, 1, 500)
        sql = "SELECT * FROM news WHERE is_junk=? AND published_at >= ?"
        args: list = [1 if junk else 0, ago(hours=48)]
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
        rows = db.query("SELECT * FROM summaries ORDER BY id DESC LIMIT ?", (_int("limit", 12, 1, 100),))
        for r in rows:
            r["content"] = json.loads(r["content"])
        return jsonify(rows)

    @app.get("/api/scores")
    def scores():
        rows = db.query(
            "SELECT s.*, m.name, m.prev_close, m.currency FROM scores_latest s "
            "LEFT JOIN price_meta m ON m.ticker = s.ticker ORDER BY s.score DESC LIMIT ?",
            (_int("limit", 25, 1, 200),),
        )
        for r in rows:
            r["components"] = json.loads(r["components"] or "{}")
            r["reasons"] = json.loads(r["reasons"] or "[]")
            r["change_pct"] = (
                (r["price"] / r["prev_close"] - 1) * 100 if r.get("price") and r.get("prev_close") else None
            )
        return jsonify(rows)

    @app.get("/api/ticker/<symbol>")
    def ticker(symbol: str):
        sym = symbol.upper()[:12]
        prices = db.query("SELECT date, close FROM prices WHERE ticker=? ORDER BY date DESC LIMIT 130", (sym,))[::-1]
        score = db.query_one("SELECT * FROM scores_latest WHERE ticker=?", (sym,))
        if score:
            score["components"] = json.loads(score["components"] or "{}")
            score["reasons"] = json.loads(score["reasons"] or "[]")
        trades_rows = db.query(
            "SELECT * FROM trades WHERE ticker=? AND COALESCE(filed_at, created_at) >= ? "
            "ORDER BY COALESCE(filed_at, created_at) DESC LIMIT 50",
            (sym, ago(days=90)),
        )
        news_rows = db.query(
            "SELECT * FROM news WHERE is_junk=0 AND tickers LIKE ? AND published_at >= ? "
            "ORDER BY published_at DESC LIMIT 30",
            (f'%"{sym}"%', ago(days=7)),
        )
        for n in news_rows:
            n["tickers"] = json.loads(n["tickers"] or "[]")
        for t in trades_rows:
            t["delay_days"] = _delay_days(t.get("tx_date"), t.get("filed_at"))
        meta = db.query_one("SELECT * FROM price_meta WHERE ticker=?", (sym,))
        return jsonify({"ticker": sym, "prices": prices, "meta": meta, "score": score,
                        "trades": trades_rows, "news": news_rows})

    @app.post("/api/run/<job>")
    def run_job(job: str):
        if not monitor.scheduler.trigger(job):
            abort(404)
        return jsonify({"ok": True, "job": job})

    return app
