"""Huis van Afgevaardigden (VS): Periodic Transaction Reports (PTR's).

Officiële, gratis bron: disclosures-clerk.house.gov. De index staat in een jaarlijks ZIP-bestand
met XML; de transacties zelf staan in PDF's. Elektronisch ingediende PDF's bevatten tekst die we
uitlezen; gescande (papieren) meldingen worden getoond met link maar zonder bedragen.
Meldplicht: tot 45 dagen na de transactie.
"""

from __future__ import annotations

import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta

import requests

from ..db import Database, iso, utcnow
from ..net import fetch
from .congress_common import parse_amount, politician_trade_id, tx_type_of, us_date_to_iso

log = logging.getLogger(__name__)

ZIP_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
PDF_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"

ASSET_MARK_RE = re.compile(r"\[(?P<atype>[A-Z]{2})\]")
TX_RE = re.compile(
    r"^\s*(?P<type>P|S\s*\(partial\)|S|E)\s+"
    r"(?P<date>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(?P<notif>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(?P<amount>\$[\d,]+\s*-\s*\$[\d,]+|Over\s+\$[\d,]+|\$[\d,]+)",
    re.IGNORECASE,
)
TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,9})\)\s*$")
OWNER_RE = re.compile(r"^(SP|JT|DC)\s+")
SKIP_LINE_RE = re.compile(r"^(F S:|S O:|D:|C:|Filing ID|Description:|Subholding Of:|Filing Status:)")
ASSET_TYPES = {
    "ST": "aandeel", "OP": "optie", "EF": "ETF", "MF": "beleggingsfonds", "CS": "obligatie",
    "GS": "staatsobligatie", "PS": "aandeel", "OT": "overig", "OI": "overig", "CT": "crypto",
}


def parse_index_xml(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    out = []
    for m in root.findall("Member"):
        def g(tag: str) -> str:
            return (m.findtext(tag) or "").strip()

        if g("FilingType") != "P":
            continue
        name = " ".join(p for p in (g("First"), g("Last"), g("Suffix")) if p)
        out.append(
            {
                "doc_id": g("DocID"),
                "name": name,
                "state_district": g("StateDst"),
                "year": g("Year"),
                "filing_date": us_date_to_iso(g("FilingDate")),
            }
        )
    return out


def extract_pdf_text(pdf_bytes: bytes) -> str:
    from pypdf import PdfReader  # lazy import: alleen nodig voor House-PDF's

    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _asset_before(segment: str) -> str:
    lines = [ln.strip() for ln in segment.split("\n")]
    lines = [ln for ln in lines if ln and not SKIP_LINE_RE.match(ln)]
    if not lines:
        return ""
    asset = lines[-1]
    # Als de ticker op een eigen regel staat ("(AAPL)"), hoort de vorige regel er ook bij
    if asset.startswith("(") and len(lines) >= 2:
        asset = lines[-2] + " " + asset
    return OWNER_RE.sub("", asset).strip()


def parse_ptr_text(text: str) -> list[dict]:
    """Haalt transacties uit de tekst van een elektronische PTR-PDF."""
    text = text.replace("\r", "")
    # Begin na de tabelkop, als die er is
    head = re.search(r"\$200\?", text)
    pos = head.end() if head else 0
    txs = []
    for m in ASSET_MARK_RE.finditer(text, pos):
        if m.start() < pos:
            continue
        after = " ".join(text[m.end(): m.end() + 300].split())
        tm = TX_RE.match(after)
        if not tm:
            continue
        asset = _asset_before(text[pos: m.start()])
        ticker = None
        tk = TICKER_RE.search(asset)
        if tk:
            ticker = tk.group(1)
            asset = asset[: tk.start()].strip().rstrip("-").strip()
        low, high, est = parse_amount(tm.group("amount"))
        txs.append(
            {
                "asset": asset,
                "ticker": ticker,
                "asset_type": ASSET_TYPES.get(m.group("atype"), m.group("atype")),
                "tx_type": tx_type_of(tm.group("type")),
                "tx_raw": tm.group("type"),
                "tx_date": us_date_to_iso(tm.group("date")),
                "notified": us_date_to_iso(tm.group("notif")),
                "value_low": low,
                "value_high": high,
                "value_usd": est,
            }
        )
        # Ga verder na het bedrag zodat de volgende asset-naam schoon begint
        consumed = _consumed_length(text, m.end(), tm.group("amount"))
        pos = consumed
    return txs


def _consumed_length(text: str, start: int, amount: str) -> int:
    """Zoekt de positie in de originele tekst waar het bedrag eindigt."""
    last_number = re.findall(r"\$[\d,]+", amount)[-1]
    idx = text.find(last_number, start)
    return idx + len(last_number) if idx >= 0 else start


class HouseSource:
    name = "house"
    MAX_PDFS_PER_RUN = 25
    LOOKBACK_DAYS = 30

    def __init__(self, cfg, db: Database, session: requests.Session):
        self.cfg = cfg
        self.db = db
        self.session = session

    def run(self) -> int:
        year = utcnow().year
        filings = []
        for y in sorted({year, (utcnow() - timedelta(days=20)).year}):
            filings.extend(self._fetch_index(y))
        cutoff = (utcnow() - timedelta(days=self.LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        todo = []
        for f in filings:
            key = f"house:{f['doc_id']}"
            if self.db.is_seen(key):
                continue
            if (f["filing_date"] or "") < cutoff:
                self.db.mark_seen(key)  # te oud om nog relevant te zijn
                continue
            todo.append(f)
        todo.sort(key=lambda f: f["filing_date"] or "", reverse=True)

        inserted = 0
        for f in todo[: self.MAX_PDFS_PER_RUN]:
            try:
                inserted += self._process(f)
            except requests.RequestException as exc:
                log.warning("House PTR %s niet opgehaald: %s", f["doc_id"], exc)
                continue
            except Exception as exc:  # noqa: BLE001 - kapotte PDF mag de rest niet tegenhouden
                log.warning("House PTR %s niet leesbaar: %s", f["doc_id"], exc)
            self.db.mark_seen(f"house:{f['doc_id']}")
        return inserted

    def _fetch_index(self, year: int) -> list[dict]:
        state_key = f"house_zip_etag_{year}"
        headers = {}
        cached = self.db.get_state(state_key) or {}
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if cached.get("last_modified"):
            headers["If-Modified-Since"] = cached["last_modified"]
        resp = fetch(self.session, ZIP_URL.format(year=year), headers=headers, timeout=60)
        if resp.status_code == 304:
            return cached.get("filings", [])
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            xml_name = next((n for n in zf.namelist() if n.lower().endswith(".xml")), None)
            if not xml_name:
                return []
            filings = parse_index_xml(zf.read(xml_name))
        # Alleen recente filings in de cache bewaren om de state klein te houden
        cutoff = (utcnow() - timedelta(days=self.LOOKBACK_DAYS + 5)).strftime("%Y-%m-%d")
        recent = [f for f in filings if (f["filing_date"] or "") >= cutoff]
        self.db.set_state(
            state_key,
            {
                "etag": resp.headers.get("ETag"),
                "last_modified": resp.headers.get("Last-Modified"),
                "filings": recent,
            },
        )
        return recent

    def _process(self, f: dict) -> int:
        url = PDF_URL.format(year=f["year"] or utcnow().year, doc_id=f["doc_id"])
        resp = fetch(self.session, url, timeout=60)
        resp.raise_for_status()
        text = extract_pdf_text(resp.content)
        txs = parse_ptr_text(text) if text.strip() else []
        role = f"Afgevaardigde ({f['state_district']})" if f["state_district"] else "Afgevaardigde"
        filed_at = iso(datetime.strptime(f["filing_date"], "%Y-%m-%d")) if f["filing_date"] else None
        trades = []
        if not txs:
            # Gescande melding of onleesbaar: toon als melding zonder details
            trades.append(
                {
                    "id": f"house:{f['doc_id']}:doc",
                    "source": "house",
                    "actor_name": f["name"],
                    "actor_role": role,
                    "actor_type": "politician",
                    "tx_type": "other",
                    "filed_at": filed_at,
                    "url": url,
                    "note": "Papieren/gescande melding: open de PDF voor details",
                }
            )
        for tx in txs:
            trades.append(
                {
                    "id": politician_trade_id(f["name"], tx["ticker"], tx["tx_date"], tx["tx_type"],
                                              tx["value_low"], tx["asset"]),
                    "source": "house",
                    "actor_name": f["name"],
                    "actor_role": role,
                    "actor_type": "politician",
                    "ticker": tx["ticker"],
                    "company": tx["asset"],
                    "asset_type": tx["asset_type"],
                    "tx_type": tx["tx_type"],
                    "tx_code": tx["tx_raw"],
                    "value_usd": tx["value_usd"],
                    "value_low": tx["value_low"],
                    "value_high": tx["value_high"],
                    "tx_date": tx["tx_date"],
                    "filed_at": filed_at,
                    "url": url,
                }
            )
        return len(self.db.insert_trades(trades))
