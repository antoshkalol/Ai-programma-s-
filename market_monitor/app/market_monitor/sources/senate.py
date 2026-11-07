"""Amerikaanse Senaat: Periodic Transaction Reports via efdsearch.senate.gov (officieel, gratis).

De site vraagt eerst om akkoord met de gebruiksvoorwaarden (alleen niet-commercieel gebruik /
geen kredietbeoordeling); daarna zijn elektronische meldingen als HTML-tabel op te vragen.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from html import unescape
from html.parser import HTMLParser

import requests

from ..db import Database, iso, utcnow
from ..net import fetch
from .congress_common import parse_amount, politician_trade_id, tx_type_of, us_date_to_iso

log = logging.getLogger(__name__)

BASE = "https://efdsearch.senate.gov"
HOME = BASE + "/search/home/"
SEARCH = BASE + "/search/"
DATA = BASE + "/search/report/data/"
CSRF_RE = re.compile(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"')
LINK_RE = re.compile(r'href="(?P<href>/search/view/(?P<kind>ptr|paper)/(?P<uuid>[0-9a-fA-F\-]+)/?)"[^>]*>(?P<title>[^<]*)<')


class _TableParser(HTMLParser):
    """Minimalistische parser die alle <tr>-rijen van de eerste tabel met <tbody> verzamelt."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self.headers: list[str] = []
        self._in_cell = False
        self._row: list[str] | None = None
        self._cell: list[str] = []
        self._in_thead = False

    def handle_starttag(self, tag, attrs):
        if tag == "thead":
            self._in_thead = True
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._in_cell = True
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "thead":
            self._in_thead = False
        elif tag in ("td", "th") and self._in_cell and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._in_cell = False
        elif tag == "tr" and self._row is not None:
            if self._in_thead:
                self.headers = self._row
            elif self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._in_cell:
            self._cell.append(data)


def parse_ptr_html(html: str) -> list[dict]:
    """Parseert de transactietabel van een elektronische Senaat-PTR."""
    p = _TableParser()
    p.feed(html)
    headers = [h.lower() for h in p.headers]

    def col(row: list[str], name: str, default_idx: int) -> str:
        # Exacte kolomnaam eerst ("type" mag niet "asset type" pakken)
        for i, h in enumerate(headers):
            if h == name and i < len(row):
                return row[i]
        for i, h in enumerate(headers):
            if name in h and i < len(row):
                return row[i]
        return row[default_idx] if default_idx < len(row) else ""

    out = []
    for row in p.rows:
        if len(row) < 8:
            continue
        ticker = col(row, "ticker", 3).strip()
        ticker = None if ticker in ("", "--", "N/A") else ticker.split()[0].upper()
        low, high, est = parse_amount(col(row, "amount", 7))
        out.append(
            {
                "tx_date": us_date_to_iso(col(row, "transaction date", 1)),
                "owner": col(row, "owner", 2),
                "ticker": ticker,
                "asset": unescape(col(row, "asset name", 4)),
                "asset_type": col(row, "asset type", 5),
                "tx_raw": col(row, "type", 6),
                "tx_type": tx_type_of(col(row, "type", 6)),
                "value_low": low,
                "value_high": high,
                "value_usd": est,
                "comment": col(row, "comment", 8) if len(row) > 8 else "",
            }
        )
    return out


def parse_search_rows(data: dict) -> list[dict]:
    out = []
    for row in data.get("data", []):
        if len(row) < 5:
            continue
        first, last, office, link_html, filed = row[0], row[1], row[2], row[3], row[4]
        m = LINK_RE.search(link_html)
        if not m:
            continue
        out.append(
            {
                "name": " ".join(f"{first} {last}".split()).title(),
                "office": office,
                "kind": m.group("kind"),
                "uuid": m.group("uuid"),
                "url": BASE + m.group("href"),
                "title": unescape(m.group("title")).strip(),
                "filed": us_date_to_iso(filed),
            }
        )
    return out


class SenateSource:
    name = "senate"
    MAX_REPORTS_PER_RUN = 20
    LOOKBACK_DAYS = 30

    def __init__(self, cfg, db: Database, session: requests.Session):
        self.cfg = cfg
        self.db = db
        self.session = session

    def _agree(self) -> str:
        home = fetch(self.session, HOME)
        home.raise_for_status()
        m = CSRF_RE.search(home.text)
        if not m:
            raise RuntimeError("CSRF-token van efdsearch.senate.gov niet gevonden")
        resp = fetch(
            self.session,
            HOME,
            method="POST",
            data={"prohibition_agreement": "1", "csrfmiddlewaretoken": m.group(1)},
            headers={"Referer": HOME},
        )
        resp.raise_for_status()
        return self.session.cookies.get("csrftoken") or m.group(1)

    def run(self) -> int:
        token = self._agree()
        start = (utcnow() - timedelta(days=self.LOOKBACK_DAYS)).strftime("%m/%d/%Y 00:00:00")
        resp = fetch(
            self.session,
            DATA,
            method="POST",
            data={
                "start": "0",
                "length": "100",
                "report_types": "[11]",
                "filer_types": "[]",
                "submitted_start_date": start,
                "submitted_end_date": "",
                "candidate_state": "",
                "senator_state": "",
                "office_id": "",
                "first_name": "",
                "last_name": "",
                "draw": "1",
                "order[0][column]": "4",
                "order[0][dir]": "desc",
            },
            headers={"Referer": SEARCH, "X-CSRFToken": token},
        )
        resp.raise_for_status()
        reports = [r for r in parse_search_rows(resp.json()) if not self.db.is_seen(f"senate:{r['uuid']}")]

        inserted = 0
        for r in reports[: self.MAX_REPORTS_PER_RUN]:
            try:
                inserted += self._process(r)
            except requests.RequestException as exc:
                log.warning("Senaat-melding %s niet opgehaald: %s", r["uuid"], exc)
                continue
            self.db.mark_seen(f"senate:{r['uuid']}")
        return inserted

    def _process(self, r: dict) -> int:
        filed_at = iso(utcnow()) if not r["filed"] else r["filed"] + "T00:00:00Z"
        base = {
            "source": "senate",
            "actor_name": r["name"],
            "actor_role": "Senator",
            "actor_type": "politician",
            "filed_at": filed_at,
            "url": r["url"],
        }
        if r["kind"] == "paper":
            return len(self.db.insert_trades([{**base, "id": f"senate:{r['uuid']}:doc", "tx_type": "other",
                                                "note": "Papieren melding: open de link voor details"}]))
        page = fetch(self.session, r["url"], headers={"Referer": SEARCH})
        page.raise_for_status()
        trades = []
        for tx in parse_ptr_html(page.text):
            trades.append(
                {
                    **base,
                    "id": politician_trade_id(r["name"], tx["ticker"], tx["tx_date"], tx["tx_type"],
                                              tx["value_low"], tx["asset"]),
                    "ticker": tx["ticker"],
                    "company": tx["asset"],
                    "asset_type": tx["asset_type"],
                    "tx_type": tx["tx_type"],
                    "tx_code": tx["tx_raw"],
                    "value_usd": tx["value_usd"],
                    "value_low": tx["value_low"],
                    "value_high": tx["value_high"],
                    "tx_date": tx["tx_date"],
                    "note": f"Eigenaar: {tx['owner']}" if tx["owner"] and tx["owner"].lower() != "self" else None,
                }
            )
        return len(self.db.insert_trades(trades))
