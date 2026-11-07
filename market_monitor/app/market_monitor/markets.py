"""Gedeelde gegevens per markt (aandelen / crypto)."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).parent / "data"


@lru_cache(maxsize=1)
def crypto_data() -> dict:
    return json.loads((DATA / "crypto.json").read_text(encoding="utf-8"))


def excluded_coins() -> set[str]:
    """Stablecoins en afgeleide tokens (wrapped/staked) worden niet beoordeeld."""
    d = crypto_data()
    return set(d["stablecoins"]) | set(d["derived_tokens"])


def crypto_proxies() -> dict[str, str | None]:
    """Aandelen/ETF's die crypto volgen -> onderliggende munt (None = crypto-sector in het algemeen)."""
    return dict(crypto_data()["proxies"])


def coin_names() -> dict[str, str]:
    """Symbool -> eerste (nette) naam, bv. BTC -> Bitcoin."""
    out: dict[str, str] = {}
    for name, sym in crypto_data()["aliases"].items():
        if sym and sym not in out and name[0].isupper() and name != sym:
            out[sym] = name
    return out
