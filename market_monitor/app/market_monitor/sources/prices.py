"""Aandelenkoersen (dagslotkoersen, ~6 maanden) uit meerdere gratis bronnen, met reserves.

Volgorde per aandeel:
  1. Twelve Data   (alleen met een gratis API-sleutel; betrouwbaar, 8 verzoeken per minuut)
  2. Yahoo Finance (geen sleutel; blokkeert soms met 'te veel verzoeken')
  3. Nasdaq        (geen sleutel; alleen Amerikaanse aandelen en ETF's)
  4. Stooq         (geen sleutel)
Geeft een bron 'te veel verzoeken' (429) of 'geweigerd' (401/403), dan wordt die bron 30 minuten
overgeslagen. Lukt geen enkele bron, dan staat in de status welke bron wat terugstuurde.

Hoe vaak (om binnen gratis limieten te blijven):
  * tijdens beurstijd in New York: volglijst, aanbevolen aandeel en sterkste kandidaten elk kwartier, rest elk uur
  * buiten beurstijd: één keer na het slot (de koers verandert dan niet)
  * Twelve Data: maximaal 750 verzoeken per dag (gratis limiet is 800)
"""

from __future__ import annotations

import csv
import io
import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from ..db import Database, iso, parse_iso, utcnow
from ..net import fetch

log = logging.getLogger(__name__)

YAHOO = "https://{host}/v8/finance/chart/{sym}?range=6mo&interval=1d"
NASDAQ = ("https://api.nasdaq.com/api/quote/{sym}/historical?assetclass={cls}&fromdate={start}"
          "&limit=400&todate={end}")
STOOQ = "https://stooq.com/q/d/l/?s={sym}&i=d"
TWELVE = "https://api.twelvedata.com/time_series?symbol={sym}&interval=1day&outputsize=150&apikey={key}"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
NON_US = (".AS", ".DE", ".PA", ".L", ".KS", ".BR", ".MI", ".SW")
BLOCK_SECONDS = 30 * 60
MAX_TICKERS = 40
TD_DAILY_BUDGET = 750


def _ny_tz():
    try:
        return ZoneInfo("America/New_York")
    except Exception:  # noqa: BLE001 - zonder tijdzonedata: benadering (EST)
        return timezone(timedelta(hours=-5))


def us_market_open(now: datetime) -> bool:
    """Ma-vr 9:30-16:15 New Yorkse tijd (inclusief een kwartier na het slot voor de slotkoers)."""
    t = now.astimezone(_ny_tz())
    if t.weekday() >= 5:
        return False
    minutes = t.hour * 60 + t.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60 + 15


def last_close(now: datetime) -> datetime:
    """Tijdstip (UTC) van het laatste beursslot (16:15 New York op een werkdag)."""
    tz = _ny_tz()
    t = now.astimezone(tz)
    d = t.date()
    while True:
        close = datetime(d.year, d.month, d.day, 16, 15, tzinfo=tz)
        if close.weekday() < 5 and close <= t:
            return close.astimezone(timezone.utc)
        d -= timedelta(days=1)


# --------------------------------------------------------------------------- parsers
def parse_yahoo(data: dict) -> dict | None:
    res = (data.get("chart") or {}).get("result") or []
    if not res:
        return None
    r = res[0]
    meta = r.get("meta", {})
    ts = r.get("timestamp") or []
    closes = (((r.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    series = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        day = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")
        series.append((day, float(c)))
    if not series:
        return None
    return {
        "series": series,
        "name": meta.get("longName") or meta.get("shortName"),
        "currency": meta.get("currency"),
        "last_price": meta.get("regularMarketPrice") or series[-1][1],
        "prev_close": meta.get("chartPreviousClose") if len(series) < 2 else series[-2][1],
    }


def _money(text) -> float | None:
    try:
        return float(str(text).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_nasdaq(data: dict) -> dict | None:
    rows = ((((data or {}).get("data") or {}).get("tradesTable") or {}).get("rows")) or []
    series = []
    for row in rows:
        close = _money(row.get("close"))
        try:
            day = datetime.strptime(row.get("date", ""), "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            continue
        if close is not None:
            series.append((day, close))
    series.sort()
    if not series:
        return None
    return {"series": series, "name": None, "currency": "USD", "last_price": series[-1][1],
            "prev_close": series[-2][1] if len(series) > 1 else None}


def parse_twelve(data: dict) -> dict | None:
    if not isinstance(data, dict) or data.get("status") == "error":
        return None
    series = []
    for v in data.get("values") or []:
        close = _money(v.get("close"))
        day = (v.get("datetime") or "")[:10]
        if close is not None and day:
            series.append((day, close))
    series.sort()
    if not series:
        return None
    meta = data.get("meta") or {}
    return {"series": series, "name": None, "currency": meta.get("currency") or "USD",
            "last_price": series[-1][1], "prev_close": series[-2][1] if len(series) > 1 else None}


def parse_stooq(text: str) -> dict | None:
    rows = list(csv.DictReader(io.StringIO(text)))
    series = []
    for row in rows[-130:]:
        try:
            series.append((row["Date"], float(row["Close"])))
        except (KeyError, ValueError):
            continue
    if not series:
        return None
    return {
        "series": series,
        "name": None,
        "currency": "USD",
        "last_price": series[-1][1],
        "prev_close": series[-2][1] if len(series) > 1 else None,
    }


class Blocked(Exception):
    """De bron weigert tijdelijk (429/401/403)."""


# --------------------------------------------------------------------------- bron
class PriceSource:
    name = "prices"

    def __init__(self, cfg, db: Database, session: requests.Session, tickers_func, priority_func=None):
        self.cfg = cfg
        self.db = db
        self.session = session
        self.tickers_func = tickers_func      # geeft de lijst tickers om bij te werken (volglijst eerst)
        self.priority_func = priority_func    # tickers die tijdens beurstijd elk kwartier ververst worden
        self.blocked_until: dict[str, float] = {}
        self.last_errors: dict[str, str] = {}
        self._yahoo_host = 0

    # ------------------------------------------------------------------ providers
    def _get(self, provider: str, url: str, headers: dict | None = None):
        resp = fetch(self.session, url, headers=headers or BROWSER_HEADERS, timeout=20)
        if resp.status_code in (401, 403, 429):
            reason = {429: "te veel verzoeken (429)", 401: "geweigerd (401)", 403: "geweigerd (403)"}[resp.status_code]
            raise Blocked(reason)
        if resp.status_code >= 400:
            raise requests.HTTPError(f"HTTP {resp.status_code}")
        return resp

    def _td_budget(self) -> dict:
        today = utcnow().strftime("%Y-%m-%d")
        b = self.db.get_state("td_budget") or {}
        return b if b.get("date") == today else {"date": today, "used": 0}

    def _twelve(self, ticker: str) -> dict | None:
        if not self.cfg.stock_price_api_key or ticker.endswith(NON_US):
            return None
        budget = self._td_budget()
        if budget["used"] >= TD_DAILY_BUDGET:
            self.last_errors["twelvedata"] = f"dagbudget van {TD_DAILY_BUDGET} verzoeken op"
            return None
        budget["used"] += 1
        self.db.set_state("td_budget", budget)
        resp = self._get("twelvedata", TWELVE.format(sym=ticker.replace("-", "."), key=self.cfg.stock_price_api_key),
                         headers={"Accept": "application/json"})
        data = resp.json()
        if isinstance(data, dict) and data.get("code") == 429:
            raise Blocked("limiet bereikt (429)")
        return parse_twelve(data)

    def _yahoo(self, ticker: str) -> dict | None:
        sym = ticker if ticker.endswith(NON_US) else ticker.replace(".", "-")
        host = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")[self._yahoo_host % 2]
        self._yahoo_host += 1
        resp = self._get("yahoo", YAHOO.format(host=host, sym=sym))
        return parse_yahoo(resp.json())

    def _nasdaq(self, ticker: str) -> dict | None:
        if ticker.endswith(NON_US):
            return None
        end = utcnow().date()
        start = end - timedelta(days=200)
        headers = {**BROWSER_HEADERS, "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}
        for cls in ("stocks", "etf"):
            resp = self._get("nasdaq", NASDAQ.format(sym=ticker.replace("-", "."), cls=cls, start=start, end=end),
                             headers=headers)
            parsed = parse_nasdaq(resp.json())
            if parsed:
                return parsed
        return None

    def _stooq(self, ticker: str) -> dict | None:
        if ticker.endswith(NON_US):
            return None
        resp = self._get("stooq", STOOQ.format(sym=f"{ticker.lower()}.us"), headers={"User-Agent": BROWSER_UA})
        if not resp.text.startswith("Date"):
            raise requests.HTTPError("geen CSV (" + resp.text[:40].strip().replace("\n", " ") + ")")
        return parse_stooq(resp.text)

    def providers(self):
        return [("twelvedata", self._twelve), ("yahoo", self._yahoo), ("nasdaq", self._nasdaq),
                ("stooq", self._stooq)]

    # ------------------------------------------------------------------ ophalen
    def fetch_one(self, ticker: str) -> dict | None:
        now = time.time()
        for name, func in self.providers():
            if self.blocked_until.get(name, 0) > now:
                continue
            try:
                parsed = func(ticker)
            except Blocked as exc:
                self.blocked_until[name] = now + BLOCK_SECONDS
                self.last_errors[name] = str(exc)
                log.warning("Koersbron %s: %s; 30 minuten overgeslagen", name, exc)
                continue
            except (requests.RequestException, ValueError) as exc:
                self.last_errors[name] = f"{type(exc).__name__}: {exc}"[:120]
                log.debug("Koersbron %s voor %s mislukt: %s", name, ticker, exc)
                continue
            if parsed and len(parsed["series"]) >= 2:
                parsed["provider"] = name
                return parsed
        return None

    def store(self, ticker: str, parsed: dict) -> None:
        self.db.executemany(
            "INSERT INTO prices(market, ticker, date, close) VALUES ('stocks', ?, ?, ?) "
            "ON CONFLICT(market, ticker, date) DO UPDATE SET close=excluded.close",
            [(ticker, d, c) for d, c in parsed["series"]],
        )
        chg = None
        if parsed["last_price"] and parsed["prev_close"]:
            chg = (parsed["last_price"] / parsed["prev_close"] - 1) * 100
        self.db.execute(
            "INSERT INTO price_meta(market, ticker, name, currency, last_price, prev_close, chg_24h, fetched_at) "
            "VALUES ('stocks', ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(market, ticker) DO UPDATE SET "
            "name=COALESCE(excluded.name, price_meta.name), currency=excluded.currency, "
            "last_price=excluded.last_price, prev_close=excluded.prev_close, chg_24h=excluded.chg_24h, "
            "fetched_at=excluded.fetched_at",
            (ticker, parsed["name"], parsed["currency"], parsed["last_price"], parsed["prev_close"], chg,
             iso(utcnow())),
        )

    @staticmethod
    def due(last: datetime | None, priority: bool, now: datetime) -> bool:
        if last is None or now - last > timedelta(hours=24):
            return True
        if us_market_open(now):
            return now - last >= (timedelta(minutes=14) if priority else timedelta(minutes=59))
        return last < last_close(now)

    def run(self) -> int:
        self.last_errors = {}
        count, used = 0, {}
        priority = list(self.priority_func()) if self.priority_func else []
        tickers = list(dict.fromkeys(priority + list(self.tickers_func())))[:MAX_TICKERS]
        prio = set(priority) if priority else set(tickers[:10])
        fetched = self.db.get_state("price_fetched") or {}
        now = utcnow()
        todo = [t for t in tickers if self.due(parse_iso(fetched.get(t)), t in prio, now)]
        for ticker in todo:
            parsed = self.fetch_one(ticker)
            if parsed:
                self.store(ticker, parsed)
                fetched[ticker] = iso(now)
                count += 1
                used[parsed["provider"]] = used.get(parsed["provider"], 0) + 1
        self.db.set_state("price_fetched", {t: v for t, v in fetched.items() if t in tickers})
        self.db.set_state("price_sources", {"used": used, "errors": self.last_errors, "at": iso(utcnow()),
                                            "skipped_up_to_date": len(tickers) - len(todo),
                                            "market_open": us_market_open(now)})
        tickers = todo
        if tickers and count == 0:
            details = "; ".join(f"{k}: {v}" for k, v in self.last_errors.items()) or "geen antwoord"
            raise RuntimeError(f"Geen aandelenkoersen opgehaald ({details})")
        if used:
            log.info("Aandelenkoersen: %s", ", ".join(f"{k} {v}x" for k, v in used.items()))
        return count
