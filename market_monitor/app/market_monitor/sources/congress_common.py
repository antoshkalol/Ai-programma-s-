"""Gedeelde hulpfuncties voor transacties van Amerikaanse Congresleden.

Politici melden geen exact bedrag maar een bandbreedte (bv. "$15,001 - $50,000").
We bewaren onder- en bovengrens en gebruiken het midden als schatting.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

AMOUNT_RE = re.compile(r"\$\s*([\d,]+)(?:\s*-\s*\$\s*([\d,]+))?")


def parse_amount(text: str | None) -> tuple[float | None, float | None, float | None]:
    """Geeft (laag, hoog, schatting). 'Over $50,000,000' -> (50000001, None, 50000001)."""
    if not text:
        return None, None, None
    t = text.replace("\n", " ")
    m = AMOUNT_RE.search(t)
    if not m:
        return None, None, None
    low = float(m.group(1).replace(",", ""))
    high = float(m.group(2).replace(",", "")) if m.group(2) else None
    if t.strip().lower().startswith("over"):
        return low + 1, None, low + 1
    if high is None:
        return low, low, low
    return low, high, round((low + high) / 2, 2)


def tx_type_of(text: str | None) -> str:
    t = (text or "").strip().lower()
    if t.startswith("p") or t.startswith("purchase") or t == "buy":
        return "buy"
    if t.startswith("s") or t.startswith("sale") or t == "sell":
        return "sell"
    return "other"


def us_date_to_iso(text: str | None) -> str | None:
    if not text:
        return None
    text = text.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def politician_trade_id(name: str, ticker: str | None, tx_date: str | None, tx_type: str,
                        value_low: float | None, asset: str | None = None) -> str:
    """Stabiele sleutel zodat dezelfde trade uit verschillende bronnen maar één keer telt."""
    last = (name or "").strip().split(" ")[-1].lower()
    key = "|".join([last, (ticker or asset or "").upper(), tx_date or "", tx_type, str(int(value_low or 0))])
    return "pol:" + hashlib.sha1(key.encode()).hexdigest()[:20]
