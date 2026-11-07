"""Eenvoudige financiële sentimentanalyse op basis van een woordenlijst (Engels + Nederlands).

Wordt gebruikt als er geen AI-sleutel is, en als snelle eerste inschatting per bericht.
Met een Anthropic-sleutel geeft de AI per halfuur een nauwkeuriger sentiment per aandeel.
"""

from __future__ import annotations

import math
import re

POSITIVE = {
    # Engels
    "beat": 1.0, "beats": 1.0, "tops": 0.8, "surge": 1.0, "surges": 1.0, "soar": 1.0, "soars": 1.0,
    "jump": 0.8, "jumps": 0.8, "rally": 0.8, "rallies": 0.8, "gain": 0.5, "gains": 0.5, "rise": 0.5,
    "rises": 0.5, "climb": 0.5, "climbs": 0.5, "record": 0.6, "upgrade": 1.0, "upgraded": 1.0,
    "outperform": 0.8, "raises guidance": 1.2, "raised guidance": 1.2, "raises forecast": 1.0,
    "buyback": 0.7, "repurchase": 0.6, "dividend increase": 0.8, "strong demand": 0.8,
    "profit": 0.4, "growth": 0.4, "approval": 0.7, "approved": 0.7, "wins": 0.6, "contract": 0.3,
    "partnership": 0.4, "expands": 0.4, "bullish": 0.8, "rebound": 0.6, "optimism": 0.5,
    "insider buying": 0.8, "acquires stake": 0.5, "breakthrough": 0.7, "accelerates": 0.4,
    # Nederlands
    "stijgt": 0.6, "stijging": 0.5, "winst": 0.4, "groei": 0.4, "omhoog": 0.5, "recordwinst": 0.8,
    "verhoogt": 0.5, "sterker": 0.5, "herstel": 0.5, "positief": 0.5, "koersdoel omhoog": 1.0,
}
NEGATIVE = {
    "miss": 1.0, "misses": 1.0, "missed": 1.0, "plunge": 1.2, "plunges": 1.2, "plummet": 1.2,
    "tumble": 1.0, "tumbles": 1.0, "sink": 0.8, "sinks": 0.8, "fall": 0.5, "falls": 0.5, "drop": 0.6,
    "drops": 0.6, "slide": 0.6, "slides": 0.6, "slump": 0.9, "downgrade": 1.0, "downgraded": 1.0,
    "underperform": 0.8, "cuts guidance": 1.2, "cut guidance": 1.2, "lowers forecast": 1.0,
    "warning": 0.7, "warns": 0.7, "lawsuit": 0.7, "sued": 0.7, "probe": 0.8, "investigation": 0.8,
    "fraud": 1.2, "recall": 0.7, "layoffs": 0.6, "bankruptcy": 1.5, "default": 0.8, "loss": 0.5,
    "losses": 0.5, "weak": 0.6, "bearish": 0.8, "selloff": 0.9, "sell-off": 0.9, "fears": 0.6,
    "crash": 1.2, "halted": 0.7, "subpoena": 0.9, "delisting": 1.2, "short seller": 0.8,
    "tariff": 0.4, "tariffs": 0.4, "insider selling": 0.5, "resigns": 0.5,
    # Nederlands
    "daalt": 0.6, "daling": 0.5, "verlies": 0.6, "omlaag": 0.5, "zwakker": 0.5, "onderzoek": 0.5,
    "faillissement": 1.5, "boete": 0.7, "ontslagen": 0.6, "negatief": 0.5, "koersdoel omlaag": 1.0,
    "winstwaarschuwing": 1.2,
}
NEGATIONS = {"not", "no", "fails", "failed", "without", "niet", "geen"}
WORD_RE = re.compile(r"[a-zA-ZÀ-ſ'\-]+")


def headline_sentiment(text: str) -> float:
    """Geeft een score tussen -1 (zeer negatief) en +1 (zeer positief)."""
    if not text:
        return 0.0
    low = text.lower()
    words = WORD_RE.findall(low)
    total = 0.0
    # Woordgroepen eerst
    for phrase, w in POSITIVE.items():
        if " " in phrase and phrase in low:
            total += w
    for phrase, w in NEGATIVE.items():
        if " " in phrase and phrase in low:
            total -= w
    for i, word in enumerate(words):
        sign = -1.0 if i > 0 and words[i - 1] in NEGATIONS else 1.0
        if word in POSITIVE:
            total += sign * POSITIVE[word]
        elif word in NEGATIVE:
            total -= sign * NEGATIVE[word]
    return round(math.tanh(total / 2.0), 3)
