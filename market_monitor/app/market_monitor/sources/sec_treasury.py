"""Bedrijven die crypto kopen voor hun balans (bv. Strategy/MicroStrategy), via SEC 8-K-meldingen.

Gebruikt de officiële full-text search van de SEC (efts.sec.gov) en leest in de melding hoeveel munten
voor welk bedrag zijn gekocht of verkocht. Gratis; dezelfde User-Agent als bij Form 4.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from html import unescape
from urllib.parse import quote

import requests

from ..db import Database, utcnow
from ..net import fetch
from ..scoring import fmt_num

log = logging.getLogger(__name__)

SEARCH = "https://efts.sec.gov/LATEST/search-index?q={q}&forms=8-K&dateRange=custom&startdt={start}&enddt={end}"
DOC = "https://www.sec.gov/Archives/edgar/data/{cik}/{adsh}/{file}"

COINS = {
    "BTC": (r"bitcoins?|BTC", '"acquired" "bitcoin"'),
    "ETH": (r"ETH|ether(?:eum)?", '"acquired" "ETH"'),
    "SOL": (r"SOL|solana", '"acquired" "SOL"'),
}
TAG_RE = re.compile(r"<[^>]+>")
NAME_RE = re.compile(r"^(.*?)\s*(?:\(([A-Z.\-, ]+)\))?\s*\(CIK \d+\)\s*$")
MONEY_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*(million|billion|mln|bn)?", re.I)
PER_UNIT_RE = re.compile(r"\s*(?:per\s+(?:bitcoin|btc|coin|token|ether|eth|sol|unit)|each)\b", re.I)


def html_to_text(html: str) -> str:
    return " ".join(unescape(TAG_RE.sub(" ", html)).split())


def parse_amounts(text: str, coin: str) -> list[dict]:
    """Zoekt zinnen als 'acquired approximately 10,000 bitcoins for approximately $1.1 billion'."""
    unit = COINS[coin][0]
    pattern = re.compile(
        r"\b(acquired|purchased|bought|sold)\s+(?:an\s+aggregate\s+of\s+|approximately\s+|a\s+total\s+of\s+)?"
        r"([\d,]+(?:\.\d+)?)\s+(?:" + unit + r")\b(.{0,220})",
        re.I | re.S,
    )
    found = []
    for m in pattern.finditer(text):
        qty = float(m.group(2).replace(",", ""))
        if qty <= 0:
            continue
        value = None
        tail = m.group(3)
        mm = MONEY_RE.search(tail)
        if mm:
            value = float(mm.group(1).replace(",", ""))
            mult = (mm.group(2) or "").lower()
            if mult in ("million", "mln"):
                value *= 1e6
            elif mult in ("billion", "bn"):
                value *= 1e9
            # Een prijs per munt ('average price of $95,000 per bitcoin') -> totaal = prijs x aantal
            if PER_UNIT_RE.match(tail[mm.end(): mm.end() + 30]):
                value *= qty
        found.append({"side": "sell" if m.group(1).lower() == "sold" else "buy", "qty": qty, "value": value})
    return found


def parse_hits(data: dict) -> list[dict]:
    out = []
    for h in (data.get("hits") or {}).get("hits", []) or []:
        src = h.get("_source", {})
        _id = h.get("_id", "")
        adsh = src.get("adsh") or _id.split(":")[0]
        file = _id.split(":", 1)[1] if ":" in _id else None
        ciks = src.get("ciks") or []
        if not (adsh and file and ciks):
            continue
        display = (src.get("display_names") or [""])[0]
        m = NAME_RE.match(display)
        name, tickers = (m.group(1).strip(), (m.group(2) or "").split(",")[0].strip()) if m else (display, "")
        out.append(
            {
                "adsh": adsh,
                "file": file,
                "cik": str(int(ciks[0])),
                "company": name,
                "company_ticker": tickers or None,
                "file_date": src.get("file_date"),
            }
        )
    return out


class SecTreasurySource:
    name = "crypto_treasury"
    MAX_DOCS_PER_RUN = 10
    LOOKBACK_DAYS = 10

    def __init__(self, cfg, db: Database, session: requests.Session):
        self.cfg = cfg
        self.db = db
        self.session = session

    def _headers(self) -> dict:
        return {"User-Agent": self.cfg.sec_user_agent.strip() or "MarketMonitor HomeAssistant-addon",
                "Accept-Encoding": "gzip, deflate"}

    def run(self) -> int:
        end = utcnow()
        start = (end - timedelta(days=self.LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        todo: dict[tuple[str, str], tuple[dict, str]] = {}
        for coin, (_, query) in COINS.items():
            url = SEARCH.format(q=quote(query), start=start, end=end.strftime("%Y-%m-%d"))
            resp = fetch(self.session, url, headers=self._headers(), timeout=30)
            resp.raise_for_status()
            for hit in parse_hits(resp.json()):
                key = (hit["adsh"], coin)
                if not self.db.is_seen(f"8k:{hit['adsh']}:{hit['file']}:{coin}"):
                    todo.setdefault(key, (hit, coin))

        inserted = 0
        for (adsh, coin), (hit, _) in list(todo.items())[: self.MAX_DOCS_PER_RUN]:
            seen_key = f"8k:{adsh}:{hit['file']}:{coin}"
            url = DOC.format(cik=hit["cik"], adsh=adsh.replace("-", ""), file=hit["file"])
            try:
                doc = fetch(self.session, url, headers=self._headers(), timeout=30)
                doc.raise_for_status()
            except requests.RequestException as exc:
                log.info("8-K %s niet opgehaald: %s", adsh, exc)
                continue
            amounts = parse_amounts(html_to_text(doc.text), coin)
            trades = []
            for i, a in enumerate(amounts[:3]):
                trades.append(
                    {
                        "id": f"8k:{adsh}:{coin}:{i}",
                        "market": "crypto",
                        "source": "sec_8k",
                        "actor_name": hit["company"],
                        "actor_role": "Bedrijf",
                        "actor_type": "company",
                        "ticker": coin,
                        "company": hit["company_ticker"],
                        "asset_type": "crypto",
                        "tx_type": a["side"],
                        "shares": a["qty"],
                        "price": round(a["value"] / a["qty"], 2) if a["value"] else None,
                        "value_usd": a["value"],
                        "value_low": a["value"],
                        "value_high": a["value"],
                        "tx_date": hit["file_date"],
                        "filed_at": f"{hit['file_date']}T00:00:00Z" if hit["file_date"] else None,
                        "url": url,
                        "note": f"{fmt_num(a['qty'])} {coin} volgens 8-K-melding",
                    }
                )
            inserted += len(self.db.insert_trades(trades))
            self.db.mark_seen(seen_key)
        return inserted
