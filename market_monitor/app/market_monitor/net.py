"""HTTP-hulpfuncties met nette rate limiting per host."""

from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import __version__

log = logging.getLogger(__name__)

DEFAULT_UA = f"MarketMonitor/{__version__} (Home Assistant add-on)"

# Maximaal aantal requests per seconde per host. sec.gov staat er 10 toe; wij blijven ruim onder.
HOST_RATES = {
    "www.sec.gov": 5.0,
    "disclosures-clerk.house.gov": 2.0,
    "efdsearch.senate.gov": 1.0,
    "api.gdeltproject.org": 0.2,
    "query1.finance.yahoo.com": 1.0,
    "query2.finance.yahoo.com": 1.0,
    "api.nasdaq.com": 1.0,
    "api.twelvedata.com": 0.13,   # gratis: 8 per minuut
    "stooq.com": 1.0,
    "news.google.com": 1.0,
    "efts.sec.gov": 4.0,
    "api.coingecko.com": 0.2,     # gratis limiet is laag: max ~12 per minuut
    "blockchain.info": 1.0,
}


class RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def wait(self, host: str) -> None:
        rate = HOST_RATES.get(host)
        if not rate:
            return
        interval = 1.0 / rate
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next.get(host, 0.0))
            self._next[host] = slot + interval
        delay = slot - now
        if delay > 0:
            time.sleep(delay)


_limiter = RateLimiter()


def make_session(user_agent: str | None = None, retry_on_429: bool = True, total: int = 3) -> requests.Session:
    """retry_on_429=False: bij 'te veel verzoeken' niet blijven proberen (koersbronnen hebben reserves)."""
    s = requests.Session()
    statuses = (429, 500, 502, 503, 504) if retry_on_429 else (500, 502, 503, 504)
    retry = Retry(
        total=total,
        backoff_factor=1.5,
        status_forcelist=statuses,
        allowed_methods=("GET", "POST"),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update(
        {
            "User-Agent": user_agent or DEFAULT_UA,
            "Accept-Encoding": "gzip, deflate",
            "Accept": "*/*",
        }
    )
    return s


def fetch(session: requests.Session, url: str, method: str = "GET", timeout: float = 30, **kwargs):
    host = urlparse(url).hostname or ""
    _limiter.wait(host)
    resp = session.request(method, url, timeout=timeout, **kwargs)
    return resp
