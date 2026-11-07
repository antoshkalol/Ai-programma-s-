"""Koersdata (dagslotkoersen, ~6 maanden) via Yahoo Finance, met Stooq als reserve.
Beide zijn gratis en vragen geen sleutel. Koersen kunnen ~15 minuten vertraagd zijn."""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone

import requests

from ..db import Database, iso, utcnow
from ..net import fetch

log = logging.getLogger(__name__)

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=6mo&interval=1d"
STOOQ = "https://stooq.com/q/d/l/?s={sym}&i=d"
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)


def parse_yahoo(data: dict) -> dict | None:
    res = (data.get("chart") or {}).get("result") or []
    if not res:
        return None
    r = res[0]
    meta = r.get("meta", {})
    ts = r.get("timestamp") or []
    closes = (((r.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    series = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        day = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")
        series.append((day, float(c)))
    if not series:
        return None
    return {
        "series": series,
        "name": meta.get("longName") or meta.get("shortName"),
        "currency": meta.get("currency"),
        "last_price": meta.get("regularMarketPrice") or series[-1][1],
        "prev_close": meta.get("chartPreviousClose") if len(series) < 2 else series[-2][1],
    }


def parse_stooq(text: str) -> dict | None:
    rows = list(csv.DictReader(io.StringIO(text)))
    series = []
    for row in rows[-130:]:
        try:
            series.append((row["Date"], float(row["Close"])))
        except (KeyError, ValueError):
            continue
    if not series:
        return None
    return {
        "series": series,
        "name": None,
        "currency": "USD",
        "last_price": series[-1][1],
        "prev_close": series[-2][1] if len(series) > 1 else None,
    }


class PriceSource:
    name = "prices"

    def __init__(self, cfg, db: Database, session: requests.Session, tickers_func):
        self.cfg = cfg
        self.db = db
        self.session = session
        self.tickers_func = tickers_func  # geeft de lijst tickers om bij te werken

    def fetch_one(self, ticker: str) -> dict | None:
        sym = ticker.replace(".", "-") if not ticker.endswith((".AS", ".DE", ".PA", ".L")) else ticker
        try:
            resp = fetch(self.session, YAHOO.format(sym=sym), headers={"User-Agent": BROWSER_UA}, timeout=20)
            if resp.ok:
                parsed = parse_yahoo(resp.json())
                if parsed:
                    return parsed
        except (requests.RequestException, ValueError) as exc:
            log.debug("Yahoo %s mislukt: %s", ticker, exc)
        if "." in ticker and ticker.split(".")[-1] in ("AS", "DE", "PA", "L"):
            return None
        try:
            resp = fetch(self.session, STOOQ.format(sym=f"{ticker.lower()}.us"), timeout=20)
            if resp.ok and resp.text.startswith("Date"):
                return parse_stooq(resp.text)
        except requests.RequestException as exc:
            log.debug("Stooq %s mislukt: %s", ticker, exc)
        return None

    def store(self, ticker: str, parsed: dict) -> None:
        self.db.executemany(
            "INSERT INTO prices(market, ticker, date, close) VALUES ('stocks', ?, ?, ?) "
            "ON CONFLICT(market, ticker, date) DO UPDATE SET close=excluded.close",
            [(ticker, d, c) for d, c in parsed["series"]],
        )
        chg = None
        if parsed["last_price"] and parsed["prev_close"]:
            chg = (parsed["last_price"] / parsed["prev_close"] - 1) * 100
        self.db.execute(
            "INSERT INTO price_meta(market, ticker, name, currency, last_price, prev_close, chg_24h, fetched_at) "
            "VALUES ('stocks', ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(market, ticker) DO UPDATE SET "
            "name=COALESCE(excluded.name, price_meta.name), currency=excluded.currency, "
            "last_price=excluded.last_price, prev_close=excluded.prev_close, chg_24h=excluded.chg_24h, "
            "fetched_at=excluded.fetched_at",
            (ticker, parsed["name"], parsed["currency"], parsed["last_price"], parsed["prev_close"], chg,
             iso(utcnow())),
        )

    def run(self) -> int:
        count = 0
        tickers = self.tickers_func()
        for ticker in tickers:
            parsed = self.fetch_one(ticker)
            if parsed:
                self.store(ticker, parsed)
                count += 1
        if tickers and count == 0:
            raise RuntimeError("Geen koersen opgehaald (Yahoo Finance en Stooq niet bereikbaar?)")
        return count
