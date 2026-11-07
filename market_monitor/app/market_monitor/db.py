"""SQLite-opslag. Eén verbinding per thread, WAL-modus zodat lezen en schrijven samengaan."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    market TEXT NOT NULL DEFAULT 'stocks',   -- stocks | crypto
    source TEXT NOT NULL,          -- sec_form4 | house | senate | quiver | sec_8k | btc_chain
    actor_name TEXT NOT NULL,
    actor_role TEXT,
    actor_type TEXT NOT NULL,      -- insider | politician | company | whale
    party TEXT,
    ticker TEXT,
    company TEXT,
    asset_type TEXT,
    tx_type TEXT NOT NULL,         -- buy | sell | other
    tx_code TEXT,
    shares REAL,
    price REAL,
    value_usd REAL,                -- puntschatting (bij politici: midden van de bandbreedte)
    value_low REAL,
    value_high REAL,
    tx_date TEXT,
    filed_at TEXT,
    planned INTEGER DEFAULT 0,     -- 10b5-1 handelsplan
    url TEXT,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_filed ON trades(market, filed_at);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at);

CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY,
    market TEXT NOT NULL DEFAULT 'stocks',
    source TEXT,
    domain TEXT,
    kind TEXT DEFAULT 'news',      -- news | post | press_release
    author TEXT,
    title TEXT NOT NULL,
    summary TEXT,
    url TEXT,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    trust REAL,
    clickbait REAL,
    credibility REAL,
    cluster_id TEXT,
    corroboration INTEGER DEFAULT 1,
    tickers TEXT DEFAULT '[]',
    sentiment REAL DEFAULT 0,
    is_junk INTEGER DEFAULT 0,
    junk_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_news_published ON news(market, published_at);
CREATE INDEX IF NOT EXISTS idx_news_cluster ON news(cluster_id);

CREATE TABLE IF NOT EXISTS prices (
    market TEXT NOT NULL DEFAULT 'stocks',
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    close REAL NOT NULL,
    PRIMARY KEY (market, ticker, date)
);

CREATE TABLE IF NOT EXISTS price_meta (
    market TEXT NOT NULL DEFAULT 'stocks',
    ticker TEXT NOT NULL,
    name TEXT,
    currency TEXT,
    last_price REAL,
    prev_close REAL,
    chg_1h REAL,
    chg_24h REAL,
    chg_7d REAL,
    volume REAL,
    market_cap REAL,
    rank INTEGER,
    fetched_at TEXT,
    PRIMARY KEY (market, ticker)
);

CREATE TABLE IF NOT EXISTS crypto_snapshots (
    coin TEXT NOT NULL,
    ts TEXT NOT NULL,
    price REAL,
    volume REAL,
    chg_1h REAL,
    chg_24h REAL,
    chg_7d REAL,
    market_cap REAL,
    rank INTEGER,
    PRIMARY KEY (coin, ts)
);

CREATE TABLE IF NOT EXISTS scores_latest (
    market TEXT NOT NULL DEFAULT 'stocks',
    ticker TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    score REAL NOT NULL,
    prob_up REAL,
    prob_source TEXT,
    timing TEXT,
    price REAL,
    components TEXT,
    reasons TEXT,
    PRIMARY KEY (market, ticker)
);

CREATE TABLE IF NOT EXISTS score_history (
    market TEXT NOT NULL DEFAULT 'stocks',
    ticker TEXT NOT NULL,
    day TEXT NOT NULL,
    score REAL NOT NULL,
    price REAL,
    PRIMARY KEY (market, ticker, day)
);

CREATE TABLE IF NOT EXISTS ticker_sentiment (
    market TEXT NOT NULL DEFAULT 'stocks',
    ticker TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sentiment REAL NOT NULL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_tsent ON ticker_sentiment(market, ticker, created_at);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT NOT NULL,
    asset TEXT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT,
    reasons TEXT,
    strength REAL,
    status TEXT NOT NULL,          -- verzonden | onderdrukt
    ai_note TEXT,
    acknowledged INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at);

CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT NOT NULL DEFAULT 'stocks',
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    created_at TEXT NOT NULL,
    method TEXT NOT NULL,          -- ai | automatisch
    content TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seen (
    key TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS job_status (
    name TEXT PRIMARY KEY,
    last_run TEXT,
    last_ok TEXT,
    last_error TEXT,
    items INTEGER DEFAULT 0
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def ago(**kwargs) -> str:
    return iso(utcnow() - timedelta(**kwargs))


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        with self._write_lock:
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if version < SCHEMA_VERSION:
                # De database is een cache van openbare data: bij een nieuwe versie bouwen we hem opnieuw op
                tables = [r[0] for r in self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                for t in tables:
                    self.conn.execute(f'DROP TABLE IF EXISTS "{t}"')
            self.conn.executescript(SCHEMA)
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
        return c

    # ------------------------------------------------------------------ basis
    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._write_lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur.rowcount

    def executemany(self, sql: str, rows: list[Iterable[Any]]) -> None:
        if not rows:
            return
        with self._write_lock:
            self.conn.executemany(sql, [tuple(r) for r in rows])
            self.conn.commit()

    # ------------------------------------------------------------------ seen/state
    def is_seen(self, key: str) -> bool:
        return self.query_one("SELECT 1 FROM seen WHERE key=?", (key,)) is not None

    def mark_seen(self, key: str) -> None:
        self.execute("INSERT OR IGNORE INTO seen(key, created_at) VALUES (?, ?)", (key, iso(utcnow())))

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM state WHERE key=?", (key,))
        if not row or row["value"] is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def set_state(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    # ------------------------------------------------------------------ trades
    TRADE_COLS = (
        "id", "market", "source", "actor_name", "actor_role", "actor_type", "party", "ticker", "company",
        "asset_type", "tx_type", "tx_code", "shares", "price", "value_usd", "value_low",
        "value_high", "tx_date", "filed_at", "planned", "url", "note", "created_at",
    )

    def insert_trades(self, trades: list[dict]) -> list[dict]:
        """Voegt nieuwe trades toe en geeft de werkelijk nieuwe terug."""
        new = []
        now = iso(utcnow())
        with self._write_lock:
            for t in trades:
                t = {**{c: None for c in self.TRADE_COLS}, **t}
                t["market"] = t.get("market") or "stocks"
                t["created_at"] = t.get("created_at") or now
                t["planned"] = int(bool(t.get("planned")))
                cur = self.conn.execute(
                    f"INSERT OR IGNORE INTO trades({','.join(self.TRADE_COLS)}) "
                    f"VALUES ({','.join('?' * len(self.TRADE_COLS))})",
                    tuple(t[c] for c in self.TRADE_COLS),
                )
                if cur.rowcount:
                    new.append(t)
            self.conn.commit()
        return new

    # ------------------------------------------------------------------ jobs
    def job_started(self, name: str) -> None:
        self.execute(
            "INSERT INTO job_status(name, last_run) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET last_run=excluded.last_run",
            (name, iso(utcnow())),
        )

    def job_finished(self, name: str, items: int = 0, error: str | None = None) -> None:
        now = iso(utcnow())
        if error:
            self.execute(
                "UPDATE job_status SET last_error=? WHERE name=?", (f"{now} {error}"[:500], name)
            )
        else:
            self.execute(
                "UPDATE job_status SET last_ok=?, last_error=NULL, items=? WHERE name=?",
                (now, items, name),
            )
