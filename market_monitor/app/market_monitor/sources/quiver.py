"""Quiver Quantitative (optioneel, betaald): kant-en-klare data over trades van Congresleden,
inclusief partij. Wordt alleen gebruikt als er een API-sleutel is ingevuld."""

from __future__ import annotations

import logging

import requests

from ..db import Database
from ..net import fetch
from .congress_common import parse_amount, politician_trade_id, tx_type_of, us_date_to_iso

log = logging.getLogger(__name__)

URL = "https://api.quiverquant.com/beta/live/congresstrading"


def _first(d: dict, *keys, default=None):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return default


def parse_quiver(rows: list[dict]) -> list[dict]:
    trades = []
    for r in rows:
        name = str(_first(r, "Representative", "Name", "Politician", default="Onbekend"))
        house = str(_first(r, "House", "Chamber", default=""))
        ticker = _first(r, "Ticker")
        ticker = str(ticker).upper() if ticker else None
        tx_type = tx_type_of(str(_first(r, "Transaction", "Type", default="")))
        rng = _first(r, "Range", "Amount")
        low, high, est = parse_amount(str(rng)) if isinstance(rng, str) else (None, None, None)
        if low is None and isinstance(rng, (int, float)):
            low = high = est = float(rng)
        tx_date = us_date_to_iso(str(_first(r, "TransactionDate", "Traded", default="")))
        filed = us_date_to_iso(str(_first(r, "ReportDate", "Filed", default="")))
        party = _first(r, "Party")
        role = "Senator" if "senate" in house.lower() else "Afgevaardigde"
        trades.append(
            {
                "id": politician_trade_id(name, ticker, tx_date, tx_type, low),
                "source": "quiver",
                "actor_name": name,
                "actor_role": role,
                "actor_type": "politician",
                "party": party,
                "ticker": ticker,
                "company": _first(r, "Description", "Company"),
                "asset_type": _first(r, "TickerType", default="aandeel"),
                "tx_type": tx_type,
                "tx_code": _first(r, "Transaction"),
                "value_usd": est,
                "value_low": low,
                "value_high": high,
                "tx_date": tx_date,
                "filed_at": f"{filed}T00:00:00Z" if filed else None,
                "url": f"https://www.quiverquant.com/congresstrading/politician/{name.replace(' ', '%20')}",
            }
        )
    return trades


class QuiverSource:
    name = "quiver"

    def __init__(self, cfg, db: Database, session: requests.Session):
        self.cfg = cfg
        self.db = db
        self.session = session

    def run(self) -> int:
        resp = fetch(
            self.session,
            URL,
            headers={"Authorization": f"Bearer {self.cfg.quiver_api_key}", "Accept": "application/json"},
        )
        resp.raise_for_status()
        rows = resp.json()
        if not isinstance(rows, list):
            return 0
        return len(self.db.insert_trades(parse_quiver(rows[:500])))
