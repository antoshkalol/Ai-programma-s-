"""Het onzin-filter: bepaalt per bericht hoe geloofwaardig het is.

Geloofwaardigheid = betrouwbaarheid van de bron
                    x (1 - clickbait-straf)
                    + bonus als meerdere onafhankelijke bronnen hetzelfde melden.
Berichten onder de drempel (instelbaar) worden als 'gefilterd' bewaard maar niet getoond.
Met een AI-sleutel markeert de AI daarnaast elk halfuur hype en geruchten (zie summary.py).
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from .db import Database, iso, parse_iso, utcnow
from .sentiment import headline_sentiment
from .tickers import TickerExtractor

log = logging.getLogger(__name__)

DATA = Path(__file__).parent / "data"

CLICKBAIT_PATTERNS = [
    r"\byou won'?t believe\b", r"\bto the moon\b", r"\b\d{3,}\s?%", r"\bmillionaires?\b",
    r"\bget rich\b", r"\bbefore it'?s too late\b", r"\bbuy (it )?now\b", r"\bsecret\b",
    r"\bshocking\b", r"\bpenny stocks?\b", r"\bmulti-?baggers?\b", r"\b(10|50|100)x\b",
    r"\bguarantee(d)?\b", r"\bdon'?t miss\b", r"\bexplod(e|es|ing)\b", r"\bskyrocket",
    r"\bgame[- ]changer\b", r"\bnext (nvidia|tesla|amazon|apple|bitcoin)\b", r"\bsponsored\b",
    r"\b(must|should) (buy|own)\b", r"\bcould (soar|surge|double|triple|explode)\b",
    r"\bset to (soar|explode|skyrocket)\b", r"\bmoonshot\b", r"\blast chance\b",
    r"\bretire (early|rich)\b", r"\bpassive income\b", r"\bunstoppable\b", r"\bno[- ]brainer\b",
    r"\bnu kopen\b", r"\bmega(winst|kans)\b", r"\bgegarandeerd\b", r"\bgeheime?\b", r"\bschokkend\b",
    r"\bexplodeert\b", r"\brijk worden\b",
]
CLICKBAIT_RE = [re.compile(p, re.IGNORECASE) for p in CLICKBAIT_PATTERNS]
QUESTION_RE = re.compile(r"^(is|should|can|will|are|could|moet|kan|gaat)\b.+\?$", re.IGNORECASE)
TOKEN_RE = re.compile(r"[a-z0-9À-ſ]+")
STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "as", "at", "by", "with", "from",
    "is", "are", "was", "be", "its", "it", "after", "over", "says", "say", "said", "new", "this",
    "that", "has", "have", "will", "into", "up", "down", "amid", "de", "het", "een", "van", "en",
    "in", "op", "voor", "met", "is", "dat", "die", "naar", "bij", "om", "s",
}


def clickbait_score(title: str) -> float:
    if not title:
        return 0.0
    score = 0.0
    hits = sum(1 for r in CLICKBAIT_RE if r.search(title))
    score += min(0.8, 0.35 * hits)
    words = [w for w in re.findall(r"[A-Za-z]{3,}", title)]
    if words:
        caps = sum(1 for w in words if w.isupper()) / len(words)
        if caps > 0.5 and len(words) >= 4:
            score += 0.3
    score += min(0.3, 0.15 * title.count("!"))
    if QUESTION_RE.match(title.strip()):
        score += 0.15
    return round(min(1.0, score), 3)


def _stem(tok: str) -> str:
    """Zeer eenvoudige stamming: jumps/jumped/jumping -> jump, raises/raised -> rais."""
    if len(tok) > 5 and tok.endswith("ing"):
        tok = tok[:-3]
    elif len(tok) > 4 and tok.endswith("ed"):
        tok = tok[:-2]
    elif len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        tok = tok[:-1]
    if len(tok) > 3 and tok.endswith("e"):
        tok = tok[:-1]
    return tok


def title_tokens(title: str) -> set[str]:
    return {_stem(t) for t in TOKEN_RE.findall((title or "").lower()) if t not in STOPWORDS and len(t) > 1}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class SourceTrust:
    def __init__(self, cfg):
        data = json.loads((DATA / "sources.json").read_text(encoding="utf-8"))
        self.default = float(data["default"])
        self.domains: dict[str, float] = {k.lower(): float(v) for k, v in data["domains"].items()}
        self.press = set(data["press_release_domains"])
        self.press_trust = float(data["press_release_trust"])
        self.x_trust = float(data["trusted_x_author_trust"])
        self.blocked = {d.lower() for d in data.get("blocked", [])}
        for d in getattr(cfg, "trusted_domains", []) or []:
            self.domains[d.lower().removeprefix("www.")] = 0.9
        for d in getattr(cfg, "blocked_domains", []) or []:
            self.blocked.add(d.lower().removeprefix("www."))

    def _match(self, domain: str, table) -> str | None:
        parts = domain.split(".")
        for i in range(len(parts) - 1):
            cand = ".".join(parts[i:])
            if cand in table:
                return cand
        return None

    def is_blocked(self, domain: str) -> bool:
        return bool(domain) and self._match(domain, self.blocked) is not None

    def is_press_release(self, domain: str) -> bool:
        return bool(domain) and self._match(domain, self.press) is not None

    def trust(self, domain: str, author: str | None = None, trusted_authors: set[str] | None = None) -> float:
        if author and trusted_authors and author.lower() in trusted_authors:
            return self.x_trust
        if not domain:
            return self.default
        if self.is_press_release(domain):
            return self.press_trust
        m = self._match(domain, self.domains)
        return self.domains[m] if m else self.default


def credibility(trust: float, clickbait: float, corroboration: int) -> float:
    bonus = min(0.24, 0.08 * max(0, corroboration - 1))
    return round(max(0.0, min(1.0, trust * (1 - 0.6 * clickbait) + bonus)), 3)


class NewsIngestor:
    """Verwerkt ruwe nieuwsitems: filter, tickers, sentiment, clustering en opslag."""

    MAX_AGE_HOURS = 72
    CLUSTER_WINDOW_HOURS = 36
    CLUSTER_THRESHOLD = 0.5

    def __init__(self, cfg, db: Database, extractor: TickerExtractor):
        self.cfg = cfg
        self.db = db
        self.extractor = extractor
        self.trust = SourceTrust(cfg)

    def __call__(self, items: list[dict], trusted_authors: set[str] | None = None) -> int:
        if not items:
            return 0
        now = utcnow()
        min_time = now - timedelta(hours=self.MAX_AGE_HOURS)

        # Ontdubbel binnen deze batch en tegen de database
        uniq: dict[str, dict] = {}
        for it in items:
            uniq.setdefault(it["id"], it)
        ids = list(uniq)
        existing = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            rows = self.db.query(f"SELECT id FROM news WHERE id IN ({','.join('?' * len(chunk))})", chunk)
            existing.update(r["id"] for r in rows)

        fresh = []
        for it in uniq.values():
            if it["id"] in existing:
                continue
            pub = parse_iso(it.get("published_at"))
            if pub and pub < min_time:
                continue
            if pub and pub > now + timedelta(minutes=10):
                pub = now
            it["published_at"] = iso(pub or now)
            fresh.append(it)
        if not fresh:
            return 0

        # Bestaande clusters van de afgelopen uren laden
        recent = self.db.query(
            "SELECT id, cluster_id, title, domain FROM news WHERE published_at >= ?",
            (iso(now - timedelta(hours=self.CLUSTER_WINDOW_HOURS)),),
        )
        clusters: dict[str, dict] = {}
        token_index: dict[str, set[str]] = defaultdict(set)
        for r in recent:
            cid = r["cluster_id"] or r["id"]
            c = clusters.setdefault(cid, {"tokens": title_tokens(r["title"]), "domains": set()})
            c["domains"].add(r["domain"] or "")
            for tok in c["tokens"]:
                token_index[tok].add(cid)

        touched: set[str] = set()
        rows = []
        fetched = iso(now)
        for it in fresh:
            domain = (it.get("domain") or "").lower()
            author = it.get("author")
            trust = self.trust.trust(domain, author, trusted_authors)
            kind = it.get("kind", "news")
            if kind == "news" and self.trust.is_press_release(domain):
                kind = "press_release"
            cb = clickbait_score(it["title"])
            text = f"{it['title']} {it.get('summary', '')}"
            tickers = self.extractor.extract(text)
            sent = headline_sentiment(it["title"])

            toks = title_tokens(it["title"])
            cid = self._find_cluster(toks, clusters, token_index)
            if cid is None:
                cid = it["id"]
                clusters[cid] = {"tokens": toks, "domains": set()}
                for tok in toks:
                    token_index[tok].add(cid)
            clusters[cid]["domains"].add(domain)
            touched.add(cid)

            junk_reason = None
            if self.trust.is_blocked(domain):
                junk_reason = "Geblokkeerde bron"
            elif cb >= 0.7:
                junk_reason = "Clickbait/hype"
            rows.append(
                (
                    it["id"], it.get("source"), domain, kind, author, it["title"][:300],
                    (it.get("summary") or "")[:800], it.get("url"), it["published_at"], fetched,
                    trust, cb, None, cid, 1, json.dumps(tickers), sent,
                    1 if junk_reason else 0, junk_reason,
                )
            )

        self.db.executemany(
            "INSERT OR IGNORE INTO news(id, source, domain, kind, author, title, summary, url, published_at,"
            " fetched_at, trust, clickbait, credibility, cluster_id, corroboration, tickers, sentiment,"
            " is_junk, junk_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.update_clusters(touched)
        return len(rows)

    def _find_cluster(self, toks: set[str], clusters, token_index) -> str | None:
        if len(toks) < 3:
            return None
        counts: dict[str, int] = defaultdict(int)
        for tok in toks:
            for cid in token_index.get(tok, ()):
                counts[cid] += 1
        best, best_score = None, 0.0
        for cid, shared in counts.items():
            if shared < 3:
                continue
            s = jaccard(toks, clusters[cid]["tokens"])
            # Korte koppen met maar 3 gedeelde woorden ("X earnings beat estimates" vs "Y earnings beat
            # estimates") zijn vaak verschillende verhalen: dan moet de kop vrijwel identiek zijn.
            ok = (shared >= 4 and s >= self.CLUSTER_THRESHOLD) or s >= 0.75
            if ok and s > best_score:
                best, best_score = cid, s
        return best

    def update_clusters(self, cluster_ids: set[str]) -> None:
        """Herberekent bevestiging (aantal bronnen) en geloofwaardigheid voor de clusters."""
        threshold = self.cfg.min_news_credibility
        updates = []
        for cid in cluster_ids:
            members = self.db.query(
                "SELECT id, domain, trust, clickbait, is_junk, junk_reason FROM news WHERE cluster_id=?", (cid,)
            )
            n_domains = len({m["domain"] for m in members if m["domain"]})
            for m in members:
                cred = credibility(m["trust"] or 0, m["clickbait"] or 0, n_domains)
                junk, reason = m["is_junk"], m["junk_reason"]
                if reason in (None, "Te weinig geloofwaardig"):
                    junk = 1 if cred < threshold else 0
                    reason = "Te weinig geloofwaardig" if junk else None
                updates.append((n_domains, cred, junk, reason, m["id"]))
        self.db.executemany(
            "UPDATE news SET corroboration=?, credibility=?, is_junk=?, junk_reason=? WHERE id=?", updates
        )

    def mark_unreliable(self, ids: list[str], reason: str = "AI: hype of gerucht") -> None:
        self.db.executemany("UPDATE news SET is_junk=1, junk_reason=? WHERE id=?", [(reason, i) for i in ids])
