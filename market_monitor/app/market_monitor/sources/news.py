"""Nieuwsbronnen: RSS-feeds van betrouwbare media, Google News (alle grote media, incl. Reuters
en Bloomberg) en GDELT (monitort nieuws wereldwijd). Alles komt via de filter in filtering.py."""

from __future__ import annotations

import calendar
import hashlib
import logging
import re
from datetime import datetime, timezone
from html import unescape
from urllib.parse import quote_plus, urlparse

import feedparser
import requests

from ..db import iso
from ..markets import coin_names
from ..net import fetch

log = logging.getLogger(__name__)

# Gecureerde feeds. Vertrouwen per domein staat in data/sources.json.
DEFAULT_FEEDS = [
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("MarketWatch Markets", "https://feeds.content.dowjones.io/public/rss/mw_marketpulse"),
    ("WSJ Markets", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("CNBC Finance", "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
    ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("Financial Times", "https://www.ft.com/markets?format=rss"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("SEC", "https://www.sec.gov/news/pressreleases.rss"),
    ("NOS Economie", "https://feeds.nos.nl/nosnieuwseconomie"),
    ("Seeking Alpha", "https://seekingalpha.com/market_currents.xml"),
]

CRYPTO_FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("The Block", "https://www.theblock.co/rss.xml"),
    ("Blockworks", "https://blockworks.co/feed"),
    ("Decrypt", "https://decrypt.co/feed"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Bitcoin Magazine", "https://bitcoinmagazine.com/.rss/full/"),
    ("CryptoSlate", "https://cryptoslate.com/feed/"),
    ("DL News", "https://www.dlnews.com/arc/outboundfeeds/rss/"),
]

TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str | None, limit: int = 500) -> str:
    if not text:
        return ""
    t = " ".join(unescape(TAG_RE.sub(" ", text)).split())
    return t[:limit]


def domain_of(url: str | None) -> str:
    if not url:
        return ""
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def item_id(url: str, title: str) -> str:
    return hashlib.sha1((url or title).encode("utf-8", "ignore")).hexdigest()[:24]


def _entry_time(entry) -> str | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return iso(datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc))
    return None


def parse_rss(content: bytes | str, feed_name: str, google_news: bool = False) -> list[dict]:
    parsed = feedparser.parse(content)
    items = []
    for e in parsed.entries:
        title = strip_html(e.get("title"), 300)
        link = e.get("link") or ""
        if not title:
            continue
        source_name = feed_name
        domain = domain_of(link)
        if google_news:
            # Google News: echte bron staat in <source url="...">; titel eindigt op " - Bron"
            src = e.get("source") or {}
            src_url = src.get("href") or src.get("url") or ""
            source_name = src.get("title") or source_name
            domain = domain_of(src_url) or domain
            suffix = f" - {source_name}"
            if title.endswith(suffix):
                title = title[: -len(suffix)].strip()
        items.append(
            {
                "id": item_id(link, title),
                "source": source_name,
                "domain": domain,
                "kind": "news",
                "title": title,
                "summary": "" if google_news else strip_html(e.get("summary")),
                "url": link,
                "published_at": _entry_time(e),
            }
        )
    return items


def parse_gdelt(data: dict) -> list[dict]:
    items = []
    for a in data.get("articles", []) or []:
        url = a.get("url") or ""
        title = " ".join((a.get("title") or "").split())
        if not url or not title:
            continue
        seen = a.get("seendate") or ""
        published = None
        try:
            published = iso(datetime.strptime(seen, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc))
        except ValueError:
            pass
        items.append(
            {
                "id": item_id(url, title),
                "source": a.get("domain") or domain_of(url),
                "domain": (a.get("domain") or domain_of(url)).lower().removeprefix("www."),
                "kind": "news",
                "title": title[:300],
                "summary": "",
                "url": url,
                "published_at": published,
                "language": a.get("language"),
            }
        )
    return items


class RssSource:
    name = "rss"

    def __init__(self, cfg, session: requests.Session, ingest, market: str = "stocks"):
        self.cfg = cfg
        self.session = session
        self.ingest = ingest
        self.market = market

    def feeds(self) -> list[tuple[str, str]]:
        if self.market == "crypto":
            feeds, extra = list(CRYPTO_FEEDS), self.cfg.extra_crypto_rss_feeds
        else:
            feeds, extra = list(DEFAULT_FEEDS), self.cfg.extra_rss_feeds
        for url in extra:
            feeds.append((domain_of(url) or "Eigen feed", url))
        return feeds

    def run(self) -> int:
        items: list[dict] = []
        feeds = self.feeds()
        errors = []
        for name, url in feeds:
            try:
                resp = fetch(self.session, url, timeout=20)
                resp.raise_for_status()
                items.extend(parse_rss(resp.content, name))
            except requests.RequestException as exc:
                log.info("Feed %s niet bereikbaar: %s", name, exc)
                errors.append(f"{name}: {type(exc).__name__}")
        if feeds and len(errors) == len(feeds):
            raise RuntimeError("Geen enkele RSS-feed bereikbaar (" + "; ".join(errors[:3]) + ")")
        return self.ingest(items)


class GoogleNewsSource:
    name = "google_news"
    URL = "https://news.google.com/rss/search?q={q}+when:1h&hl=en-US&gl=US&ceid=US:en"

    def __init__(self, cfg, session: requests.Session, ingest, market: str = "stocks"):
        self.cfg = cfg
        self.session = session
        self.ingest = ingest
        self.market = market

    def queries(self) -> list[str]:
        base = list(self.cfg.news_queries(self.market))
        if self.market == "crypto":
            names = coin_names()
            base += [f"{names.get(s, s)} crypto" for s in self.cfg.crypto_watchlist[:8]]
        else:
            base += [f"{t} stock" for t in self.cfg.stock_watchlist[:10]]
        return list(dict.fromkeys(base))

    def run(self) -> int:
        items: list[dict] = []
        queries = self.queries()
        failed = 0
        last_exc = None
        for q in queries:
            try:
                resp = fetch(self.session, self.URL.format(q=quote_plus(q)), timeout=20)
                resp.raise_for_status()
                items.extend(parse_rss(resp.content, "Google News", google_news=True))
            except requests.RequestException as exc:
                log.info("Google News '%s' mislukt: %s", q, exc)
                failed += 1
                last_exc = exc
        if queries and failed == len(queries):
            raise RuntimeError(f"Google News niet bereikbaar: {type(last_exc).__name__}")
        return self.ingest(items)


class GdeltSource:
    name = "gdelt"
    URL = (
        "https://api.gdeltproject.org/api/v2/doc/doc?query={q}&mode=artlist&format=json"
        "&maxrecords=150&timespan=30min&sort=datedesc"
    )

    def __init__(self, cfg, session: requests.Session, ingest, market: str = "stocks"):
        self.cfg = cfg
        self.session = session
        self.ingest = ingest
        self.market = market

    def run(self) -> int:
        terms = " OR ".join(f'"{q}"' if " " in q else q for q in self.cfg.news_queries(self.market)[:8])
        query = f"({terms}) sourcelang:english"
        resp = fetch(self.session, self.URL.format(q=quote_plus(query)), timeout=30)
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError:
            # GDELT geeft bij een ongeldige query platte tekst terug
            log.warning("GDELT antwoord geen JSON: %s", resp.text[:200])
            return 0
        return self.ingest(parse_gdelt(data))
