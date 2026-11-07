"""SEC EDGAR Form 4: aan- en verkopen door bestuurders, directeuren en grootaandeelhouders (>10%).

Meldplicht: binnen 2 werkdagen na de transactie. Gratis, officiële bron.
De SEC eist een User-Agent met naam en e-mailadres.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

import requests

from ..db import Database, iso, parse_iso
from ..net import fetch

log = logging.getLogger(__name__)

ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
FEED_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&company=&dateb="
    "&owner=include&start={start}&count=100&output=atom"
)
ACCESSION_RE = re.compile(r"(\d{10}-\d{2}-\d{6})")

CODE_TO_TYPE = {"P": "buy", "S": "sell"}

ENTITY_WORDS = {
    "INC", "INC.", "LLC", "L.L.C.", "LP", "L.P.", "CORP", "CORP.", "CORPORATION", "FUND", "TRUST",
    "HOLDINGS", "HOLDING", "CAPITAL", "PARTNERS", "MANAGEMENT", "GROUP", "LTD", "LTD.", "CO",
    "CO.", "ADVISORS", "ASSOCIATES", "INVESTMENTS", "FOUNDATION", "COMPANY", "BANK", "PLC",
    "N.V.", "S.A.", "AG", "SE", "LIMITED", "VENTURES", "MASTER", "OFFSHORE", "&",
}


# --------------------------------------------------------------------------- parsing
def parse_feed(xml_text: str) -> list[dict]:
    """Parseert de EDGAR 'latest filings' Atom-feed naar unieke filings."""
    root = ET.fromstring(xml_text)
    out: dict[str, dict] = {}
    for entry in root.findall("a:entry", ATOM_NS):
        id_text = (entry.findtext("a:id", default="", namespaces=ATOM_NS) or "")
        link_el = entry.find("a:link", ATOM_NS)
        href = link_el.get("href") if link_el is not None else ""
        m = ACCESSION_RE.search(id_text) or ACCESSION_RE.search(href or "")
        if not m or not href:
            continue
        acc = m.group(1)
        cat = entry.find("a:category", ATOM_NS)
        form = cat.get("term") if cat is not None else "4"
        if form not in ("4", "4/A"):
            continue
        if acc in out:
            continue
        updated = entry.findtext("a:updated", default="", namespaces=ATOM_NS)
        out[acc] = {
            "accession": acc,
            "index_url": href,
            "dir_url": href.rsplit("/", 1)[0],
            "form": form,
            "filed_at": iso(parse_iso(updated)) if updated else None,
            "title": entry.findtext("a:title", default="", namespaces=ATOM_NS),
        }
    return list(out.values())


def pick_xml_from_index(index_json: dict) -> str | None:
    items = index_json.get("directory", {}).get("item", [])
    xmls = [
        i["name"]
        for i in items
        if i.get("name", "").lower().endswith(".xml") and "index" not in i.get("name", "").lower()
    ]
    if not xmls:
        return None
    # Voorkeur voor bestanden die naar form4 verwijzen
    xmls.sort(key=lambda n: (0 if "form4" in n.lower() or "f345" in n.lower() else 1, len(n)))
    return xmls[0]


def _text(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    node = el.find(path + "/value")
    if node is None:
        node = el.find(path)
    if node is None or node.text is None:
        return None
    t = node.text.strip()
    return t or None


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def pretty_name(raw: str) -> str:
    """'MUSK ELON' -> 'Elon Musk'; entiteiten ('BERKSHIRE HATHAWAY INC') blijven in volgorde."""
    raw = " ".join(raw.split())
    parts = raw.split(" ")
    if any(p.upper() in ENTITY_WORDS for p in parts) or len(parts) < 2 or len(parts) > 4:
        return raw.title()
    last, rest = parts[0], parts[1:]
    return " ".join(rest + [last]).title()


def role_of(rel: ET.Element | None) -> str:
    if rel is None:
        return "Insider"
    roles = []
    if _bool(_text(rel, "isOfficer")):
        roles.append(_text(rel, "officerTitle") or "Officer")
    if _bool(_text(rel, "isDirector")):
        roles.append("Director")
    if _bool(_text(rel, "isTenPercentOwner")):
        roles.append("10%-aandeelhouder")
    if _bool(_text(rel, "isOther")):
        roles.append(_text(rel, "otherText") or "Overig")
    return ", ".join(roles) or "Insider"


def parse_form4(xml_text: str) -> dict:
    root = ET.fromstring(xml_text)
    issuer = root.find("issuer")
    ticker = (_text(issuer, "issuerTradingSymbol") or "").upper().strip()
    if ticker in ("", "NONE", "N/A", "NA"):
        ticker = None
    owners = []
    for ro in root.findall("reportingOwner"):
        name = _text(ro, "reportingOwnerId/rptOwnerName") or "Onbekend"
        owners.append({"name": pretty_name(name), "role": role_of(ro.find("reportingOwnerRelationship"))})

    planned_flag = _bool(_text(root, "aff10b5One"))
    footnotes = " ".join((f.text or "") for f in root.iter("footnote"))
    planned = planned_flag or "10b5-1" in footnotes

    txs = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _text(tx, "transactionCoding/transactionCode")
        shares = _float(_text(tx, "transactionAmounts/transactionShares"))
        price = _float(_text(tx, "transactionAmounts/transactionPricePerShare"))
        txs.append(
            {
                "code": code,
                "date": (_text(tx, "transactionDate") or "")[:10] or None,
                "shares": shares or 0.0,
                "price": price,
                "acq_disp": _text(tx, "transactionAmounts/transactionAcquiredDisposedCode"),
                "security": _text(tx, "securityTitle"),
            }
        )
    return {
        "document_type": _text(root, "documentType"),
        "period": _text(root, "periodOfReport"),
        "issuer_name": (_text(issuer, "issuerName") or "").strip(),
        "issuer_cik": _text(issuer, "issuerCik"),
        "ticker": ticker,
        "owners": owners,
        "planned": planned,
        "transactions": txs,
    }


def form4_to_trades(parsed: dict, accession: str, filed_at: str | None, url: str) -> list[dict]:
    """Groepeert transacties per code (P=koop, S=verkoop) tot één trade per filing."""
    groups: dict[str, dict] = {}
    for tx in parsed["transactions"]:
        code = (tx["code"] or "").upper()
        if code not in CODE_TO_TYPE or not tx["shares"]:
            continue
        g = groups.setdefault(code, {"shares": 0.0, "value": 0.0, "priced_shares": 0.0, "dates": []})
        g["shares"] += tx["shares"]
        if tx["price"]:
            g["value"] += tx["shares"] * tx["price"]
            g["priced_shares"] += tx["shares"]
        if tx["date"]:
            g["dates"].append(tx["date"])

    owners = parsed["owners"] or [{"name": "Onbekend", "role": "Insider"}]
    actor = " & ".join(o["name"] for o in owners[:2]) + (" e.a." if len(owners) > 2 else "")
    role = owners[0]["role"]
    trades = []
    for code, g in groups.items():
        avg_price = g["value"] / g["priced_shares"] if g["priced_shares"] else None
        value = g["value"] if g["value"] else None
        trades.append(
            {
                "id": f"sec:{accession}:{code}",
                "source": "sec_form4",
                "actor_name": actor,
                "actor_role": role,
                "actor_type": "insider",
                "ticker": parsed["ticker"],
                "company": parsed["issuer_name"].title() if parsed["issuer_name"].isupper() else parsed["issuer_name"],
                "asset_type": "aandeel",
                "tx_type": CODE_TO_TYPE[code],
                "tx_code": code,
                "shares": g["shares"],
                "price": round(avg_price, 4) if avg_price else None,
                "value_usd": round(value, 2) if value else None,
                "value_low": round(value, 2) if value else None,
                "value_high": round(value, 2) if value else None,
                "tx_date": max(g["dates"]) if g["dates"] else parsed.get("period"),
                "filed_at": filed_at,
                "planned": parsed["planned"],
                "url": url,
                "note": "10b5-1 handelsplan (vooraf ingepland)" if parsed["planned"] else None,
            }
        )
    return trades


# --------------------------------------------------------------------------- bron
class SecForm4Source:
    name = "sec_form4"
    MAX_FILINGS_PER_RUN = 80
    MAX_PAGES = 3

    def __init__(self, cfg, db: Database, session: requests.Session):
        self.cfg = cfg
        self.db = db
        self.session = session

    def _headers(self) -> dict:
        ua = self.cfg.sec_user_agent.strip() or "MarketMonitor HomeAssistant-addon"
        return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}

    def run(self) -> int:
        if not self.cfg.sec_headers_ok:
            log.warning(
                "sec_user_agent bevat geen e-mailadres; de SEC kan verzoeken weigeren. "
                "Vul 'Naam jouw@email.nl' in bij de add-on opties."
            )
        pending: list[dict] = []
        for page in range(self.MAX_PAGES):
            resp = fetch(self.session, FEED_URL.format(start=page * 100), headers=self._headers())
            resp.raise_for_status()
            filings = parse_feed(resp.text)
            new = [f for f in filings if not self.db.is_seen(f"sec:{f['accession']}")]
            pending.extend(new)
            if len(new) < len(filings) or not filings:
                break  # we zijn bij al verwerkte filings aangekomen

        # Oudste eerst, zodat een volgende run verder kan waar deze stopt
        pending.sort(key=lambda f: f.get("filed_at") or "")
        pending = pending[: self.MAX_FILINGS_PER_RUN]

        inserted = 0
        for filing in pending:
            key = f"sec:{filing['accession']}"
            try:
                trades = self._process(filing)
            except requests.RequestException as exc:
                log.warning("Form 4 %s niet opgehaald: %s", filing["accession"], exc)
                continue
            except ET.ParseError as exc:
                log.warning("Form 4 %s niet leesbaar: %s", filing["accession"], exc)
                trades = []
            inserted += len(self.db.insert_trades(trades))
            self.db.mark_seen(key)
        return inserted

    def _process(self, filing: dict) -> list[dict]:
        idx = fetch(self.session, filing["dir_url"] + "/index.json", headers=self._headers())
        idx.raise_for_status()
        xml_name = pick_xml_from_index(idx.json())
        if not xml_name:
            return []
        doc = fetch(self.session, f"{filing['dir_url']}/{xml_name}", headers=self._headers())
        doc.raise_for_status()
        parsed = parse_form4(doc.text)
        return form4_to_trades(parsed, filing["accession"], filing.get("filed_at"), filing["index_url"])
