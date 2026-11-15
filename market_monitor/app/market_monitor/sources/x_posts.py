"""Posts van belangrijke accounts op X.

Twee manieren (beide optioneel):
  * de officiële X API (betaald): 'recent search'-endpoint van API v2 met een bearer token (x_bearer_token)
  * een RSS-bron per account (x_rss_template), bv. een eigen RSS-bridge of Nitter-server:
    https://jouw-server/{account}/rss. Werkt alleen zolang die dienst werkt.
"""

from __future__ import annotations

import hashlib
import logging
import re
from urllib.parse import urlparse

import requests

from ..db import Database
from ..net import fetch

log = logging.getLogger(__name__)

URL = "https://api.x.com/2/tweets/search/recent"
MAX_QUERY_LEN = 480


def build_queries(accounts: list[str]) -> list[str]:
    queries, current = [], []
    for acc in accounts:
        candidate = current + [f"from:{acc}"]
        q = "(" + " OR ".join(candidate) + ") -is:retweet"
        if len(q) > MAX_QUERY_LEN and current:
            queries.append("(" + " OR ".join(current) + ") -is:retweet")
            current = [f"from:{acc}"]
        else:
            current = candidate
    if current:
        queries.append("(" + " OR ".join(current) + ") -is:retweet")
    return queries


def parse_tweets(data: dict) -> list[dict]:
    users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
    items = []
    for t in data.get("data", []) or []:
        u = users.get(t.get("author_id"), {})
        username = u.get("username", "onbekend")
        text = " ".join((t.get("text") or "").split())
        items.append(
            {
                "id": f"x:{t['id']}",
                "source": f"X @{username}",
                "domain": "x.com",
                "kind": "post",
                "author": username,
                "title": text[:300],
                "summary": text[300:800],
                "url": f"https://x.com/{username}/status/{t['id']}",
                "published_at": t.get("created_at", "").replace(".000Z", "Z") or None,
            }
        )
    return items


class XSource:
    name = "x_posts"

    def __init__(self, cfg, db: Database, session: requests.Session, ingest, market: str = "stocks"):
        self.cfg = cfg
        self.db = db
        self.session = session
        self.ingest = ingest
        self.market = market

    def run(self) -> int:
        state_key = f"x_since_id_{self.market}"
        accounts = self.cfg.x_accounts(self.market)
        since = self.db.get_state(state_key)
        newest = since
        items: list[dict] = []
        for q in build_queries(accounts):
            params = {
                "query": q,
                "max_results": "50",
                "tweet.fields": "created_at,author_id",
                "expansions": "author_id",
                "user.fields": "username,name",
            }
            if since:
                params["since_id"] = since
            resp = fetch(
                self.session,
                URL,
                params=params,
                headers={"Authorization": f"Bearer {self.cfg.x_bearer_token}"},
            )
            if resp.status_code == 429:
                log.warning("X API: limiet bereikt, volgende keer opnieuw")
                break
            resp.raise_for_status()
            data = resp.json()
            items.extend(parse_tweets(data))
            nid = data.get("meta", {}).get("newest_id")
            if nid and (not newest or int(nid) > int(newest)):
                newest = nid
        if newest:
            self.db.set_state(state_key, newest)
        return self.ingest(items, trusted_authors={a.lower() for a in accounts})


STATUS_RE = re.compile(r"/([A-Za-z0-9_]{1,30})/status(?:es)?/(\d+)")


def parse_x_rss(content: bytes | str, account: str) -> list[dict]:
    """RSS van een account (Nitter/RSS-bridge) -> posts. Links worden omgezet naar x.com."""
    from .news import _entry_time, strip_html
    import feedparser

    items = []
    for e in feedparser.parse(content).entries:
        text = strip_html(e.get("title") or e.get("summary"), 800)
        link = e.get("link") or ""
        if not text:
            continue
        if text.startswith("RT by ") or text.startswith("RT @"):
            continue          # retweets overslaan, net als bij de X API
        m = STATUS_RE.search(urlparse(link).path or "")
        if m:
            pid = f"x:{m.group(2)}"
            url = f"https://x.com/{m.group(1)}/status/{m.group(2)}"
        else:
            pid = "xrss:" + hashlib.sha1((link or text).encode()).hexdigest()[:20]
            url = link
        items.append({"id": pid, "source": f"X @{account}", "domain": "x.com", "kind": "post", "author": account,
                      "title": text[:300], "summary": text[300:800], "url": url, "published_at": _entry_time(e)})
    return items


class XRssSource:
    name = "x_rss"

    def __init__(self, cfg, session: requests.Session, ingest, market: str = "stocks"):
        self.cfg = cfg
        self.session = session
        self.ingest = ingest
        self.market = market

    def run(self) -> int:
        accounts = self.cfg.x_accounts(self.market)
        items: list[dict] = []
        errors = []
        for acc in accounts:
            url = self.cfg.x_rss_template.replace("{account}", acc)
            try:
                resp = fetch(self.session, url, timeout=20)
                resp.raise_for_status()
                items.extend(parse_x_rss(resp.content, acc))
            except requests.RequestException as exc:
                errors.append(f"{acc}: {exc.__class__.__name__}")
        if errors and len(errors) == len(accounts):
            raise RuntimeError("RSS-bron voor X geeft geen antwoord (" + "; ".join(errors[:3]) + ")")
        return self.ingest(items, trusted_authors={a.lower() for a in accounts})
