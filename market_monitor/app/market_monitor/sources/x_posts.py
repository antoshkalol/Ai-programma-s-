"""Posts van belangrijke accounts op X (optioneel; de X API is betaald).

Gebruikt het 'recent search'-endpoint van API v2 met een bearer token.
"""

from __future__ import annotations

import logging

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

    def __init__(self, cfg, db: Database, session: requests.Session, ingest):
        self.cfg = cfg
        self.db = db
        self.session = session
        self.ingest = ingest

    def run(self) -> int:
        since = self.db.get_state("x_since_id")
        newest = since
        items: list[dict] = []
        for q in build_queries(self.cfg.x_accounts):
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
            self.db.set_state("x_since_id", newest)
        return self.ingest(items, trusted_authors={a.lower() for a in self.cfg.x_accounts})
