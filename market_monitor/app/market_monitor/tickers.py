"""Herkent aandelen (tickers) en cryptomunten (symbolen) in nieuwskoppen en posts."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from .markets import crypto_data, excluded_coins

DATA = Path(__file__).parent / "data"

CASHTAG_RE = re.compile(r"(?<![A-Za-z0-9])\$([A-Z]{1,6}(?:\.[A-Z])?)\b")
EXCHANGE_RE = re.compile(
    r"\((?:NASDAQ|Nasdaq|NYSE|NYSE American|NYSEARCA|AMEX|OTC|OTCQX|TSX|LSE|Euronext)\s*:\s*([A-Z][A-Z.\-]{0,6})\)"
)
UPPER_WORD_RE = re.compile(r"\b[A-Z]{2,6}\b")

SUFFIXES_RE = re.compile(
    r"[,\s]+(inc\.?|incorporated|corp\.?|corporation|co\.?|company|ltd\.?|limited|plc|n\.v\.|nv|s\.a\.|"
    r"sa|ag|se|holdings?|group|class [abc]|common stock|/de/|/md/|the)$",
    re.IGNORECASE,
)
COMMON_WORDS = {
    "the", "first", "general", "american", "united", "international", "national", "global", "energy",
    "target", "block", "gap", "visa", "meta", "best", "match", "booking", "progressive", "sun", "life",
}


def clean_company_name(name: str) -> str:
    n = " ".join((name or "").replace(" ", " ").split())
    prev = None
    while prev != n:
        prev = n
        n = SUFFIXES_RE.sub("", n).strip(" ,.")
    if n.isupper():
        n = n.title()
    return n


class TickerExtractor:
    """market='stocks' herkent aandelen, market='crypto' herkent munten."""

    def __init__(self, db=None, watchlist: list[str] | None = None, market: str = "stocks"):
        self.market = market
        if market == "crypto":
            cfg = crypto_data()
            self.excluded = excluded_coins()
        else:
            cfg = json.loads((DATA / "companies.json").read_text(encoding="utf-8"))
            self.excluded = set()
        self.static_aliases: dict[str, str] = {k: v for k, v in cfg["aliases"].items() if v}
        self.stoplist = set(cfg["ticker_stoplist"])
        self.db = db
        self.watchlist = [t.upper() for t in (watchlist or [])]
        self._lock = threading.Lock()
        self._loaded_at = 0.0
        self._alias_re: re.Pattern | None = None
        self._aliases: dict[str, str] = {}
        self._known: set[str] = set()
        self.refresh()

    def refresh(self) -> None:
        aliases = dict(self.static_aliases)
        known = set(aliases.values()) | set(self.watchlist)
        if self.db is not None and self.market == "stocks":
            rows = self.db.query(
                "SELECT DISTINCT ticker, company FROM trades WHERE market='stocks' AND ticker IS NOT NULL "
                "AND company IS NOT NULL AND filed_at >= datetime('now', '-45 days')"
            )
            for r in rows:
                t = r["ticker"].upper()
                known.add(t)
                name = clean_company_name(r["company"])
                if len(name) >= 4 and name.lower() not in COMMON_WORDS and name not in aliases:
                    aliases[name] = t
        if self.db is not None and self.market == "crypto":
            # Top-100 munten van CoinGecko (bijgewerkt door de koersbron)
            cmap = self.db.get_state("coingecko_map") or {}
            for sym, info in cmap.items():
                if sym in self.excluded:
                    continue
                known.add(sym)
                name = info.get("name") or ""
                if len(name) >= 4 and name.lower() not in COMMON_WORDS and name not in aliases and name.upper() != sym:
                    aliases[name] = sym
        names = sorted(aliases, key=len, reverse=True)
        pattern = r"(?<![\w&])(" + "|".join(re.escape(n) for n in names) + r")(?![\w&])" if names else None
        with self._lock:
            self._aliases = aliases
            self._alias_re = re.compile(pattern) if pattern else None
            self._known = known - self.excluded
            self._loaded_at = time.time()

    def extract(self, text: str) -> list[str]:
        if not text:
            return []
        if time.time() - self._loaded_at > 1800:
            self.refresh()
        found: list[str] = []
        with self._lock:
            alias_re, aliases, known = self._alias_re, self._aliases, self._known

        def add(t: str) -> None:
            t = t.upper()
            if t in found or t in self.stoplist or t in self.excluded:
                return
            # Bij crypto alleen munten die we kennen (voorkomt $-tags van aandelen)
            if self.market == "crypto" and t not in known:
                return
            found.append(t)

        for m in CASHTAG_RE.finditer(text):
            add(m.group(1).replace(".", "-"))
        if self.market == "stocks":
            for m in EXCHANGE_RE.finditer(text):
                add(m.group(1))
        if alias_re:
            for m in alias_re.finditer(text):
                add(aliases[m.group(1)])
        for m in UPPER_WORD_RE.finditer(text):
            w = m.group(0)
            if w in known and len(w) >= 3 and w not in self.stoplist:
                add(w)
        return found[:8]
