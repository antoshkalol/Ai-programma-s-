"""Wisselkoers euro/dollar (ECB-referentiekoers, één keer per 6 uur). Het budget en de boekhouding zijn in euro;
koersen in het dashboard zijn in dollar."""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from ..db import iso, parse_iso, utcnow
from ..net import fetch

log = logging.getLogger(__name__)

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
FALLBACK = 1.10        # alleen als de ECB nog nooit bereikbaar was
STATE = "fx_eurusd"


def parse_ecb(xml: str) -> float | None:
    m = re.search(r"currency=['\"]USD['\"]\s+rate=['\"]([0-9.]+)['\"]", xml)
    return float(m.group(1)) if m else None


class FxRate:
    def __init__(self, db, session):
        self.db = db
        self.session = session

    def eurusd(self) -> float:
        """Dollars per euro (bv. 1,10). Ververst hooguit elke 6 uur."""
        st = self.db.get_state(STATE) or {}
        at = parse_iso(st.get("at"))
        if st.get("rate") and at and utcnow() - at < timedelta(hours=6):
            return st["rate"]
        try:
            r = fetch(self.session, ECB_URL, timeout=15)
            rate = parse_ecb(r.text) if r.status_code == 200 else None
            if rate and 0.5 < rate < 2.5:
                self.db.set_state(STATE, {"rate": rate, "at": iso(utcnow())})
                return rate
        except Exception as exc:  # noqa: BLE001
            log.warning("Wisselkoers ophalen mislukt: %s", exc)
        if st.get("rate"):
            # Oude koers gebruiken, maar pas over een uur opnieuw proberen
            self.db.set_state(STATE, {"rate": st["rate"], "at": iso(utcnow() - timedelta(hours=5))})
            return st["rate"]
        return FALLBACK
