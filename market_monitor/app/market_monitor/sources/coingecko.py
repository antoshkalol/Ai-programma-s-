"""Cryptokoersen via CoinGecko (gratis; optioneel een gratis 'demo'-sleutel voor ruimere limieten).

* Marktoverzicht (top 100): koers, 1u/24u/7d-verandering, volume, marktwaarde. Elke paar minuten;
  wordt bewaard als snapshot zodat de noodmelding plotselinge stijgingen en volumepieken ziet.
* Historie (180 dagen, dagelijks) per munt voor trend, RSI en instapmoment.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from ..db import Database, ago, iso, utcnow
from ..markets import excluded_coins
from ..net import fetch

log = logging.getLogger(__name__)

BASE = "https://api.coingecko.com/api/v3"
MARKETS_URL = (
    BASE + "/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=100&page=1"
    "&price_change_percentage=1h,24h,7d"
)
CHART_URL = BASE + "/coins/{id}/market_chart?vs_currency=usd&days=180&interval=daily"


def headers(cfg) -> dict:
    h = {"Accept": "application/json"}
    if cfg.coingecko_api_key:
        h["x-cg-demo-api-key"] = cfg.coingecko_api_key
    return h


def parse_markets(rows: list[dict]) -> list[dict]:
    """Normaliseert de CoinGecko /coins/markets-respons. Dubbele symbolen: de grootste munt wint."""
    out, seen = [], set()
    excluded = excluded_coins()
    for r in rows:
        sym = str(r.get("symbol") or "").upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        price = r.get("current_price")
        chg24 = r.get("price_change_percentage_24h_in_currency", r.get("price_change_percentage_24h"))
        out.append(
            {
                "symbol": sym,
                "id": r.get("id"),
                "name": r.get("name"),
                "price": price,
                "chg_1h": r.get("price_change_percentage_1h_in_currency"),
                "chg_24h": chg24,
                "chg_7d": r.get("price_change_percentage_7d_in_currency"),
                "volume": r.get("total_volume"),
                "market_cap": r.get("market_cap"),
                "rank": r.get("market_cap_rank"),
                "excluded": sym in excluded,
            }
        )
    return out


def parse_chart(data: dict) -> list[tuple[str, float]]:
    """market_chart-respons -> [(datum, slotkoers)], één per dag (laatste waarde van de dag wint)."""
    by_day: dict[str, float] = {}
    for ms, price in data.get("prices", []) or []:
        if price is None:
            continue
        day = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        by_day[day] = float(price)
    return sorted(by_day.items())


class CoinGeckoMarkets:
    name = "crypto_markets"

    def __init__(self, cfg, db: Database, session: requests.Session):
        self.cfg = cfg
        self.db = db
        self.session = session

    def run(self) -> int:
        resp = fetch(self.session, MARKETS_URL, headers=headers(self.cfg), timeout=30)
        if resp.status_code == 429:
            raise RuntimeError("CoinGecko: limiet bereikt (overweeg een gratis demo-sleutel)")
        resp.raise_for_status()
        coins = parse_markets(resp.json())
        now = iso(utcnow())
        cmap = {c["symbol"]: {"id": c["id"], "name": c["name"], "rank": c["rank"]}
                for c in coins if not c["excluded"]}
        self.db.set_state("coingecko_map", cmap)
        snaps, metas = [], []
        for c in coins:
            if c["excluded"] or c["price"] is None:
                continue
            snaps.append((c["symbol"], now, c["price"], c["volume"], c["chg_1h"], c["chg_24h"], c["chg_7d"],
                          c["market_cap"], c["rank"]))
            prev = c["price"] / (1 + c["chg_24h"] / 100) if c["chg_24h"] not in (None, -100) else None
            metas.append((c["symbol"], c["name"], c["price"], prev, c["chg_1h"], c["chg_24h"], c["chg_7d"],
                          c["volume"], c["market_cap"], c["rank"], now))
        self.db.executemany(
            "INSERT OR REPLACE INTO crypto_snapshots(coin, ts, price, volume, chg_1h, chg_24h, chg_7d, market_cap, rank)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            snaps,
        )
        self.db.executemany(
            "INSERT INTO price_meta(market, ticker, name, currency, last_price, prev_close, chg_1h, chg_24h, chg_7d,"
            " volume, market_cap, rank, fetched_at) VALUES ('crypto', ?, ?, 'USD', ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(market, ticker) DO UPDATE SET name=excluded.name, last_price=excluded.last_price,"
            " prev_close=excluded.prev_close, chg_1h=excluded.chg_1h, chg_24h=excluded.chg_24h,"
            " chg_7d=excluded.chg_7d, volume=excluded.volume, market_cap=excluded.market_cap, rank=excluded.rank,"
            " fetched_at=excluded.fetched_at",
            metas,
        )
        # Snapshots maar 3 dagen bewaren
        self.db.execute("DELETE FROM crypto_snapshots WHERE ts < ?", (ago(days=3),))
        return len(snaps)


class CoinGeckoHistory:
    name = "crypto_history"

    def __init__(self, cfg, db: Database, session: requests.Session, coins_func):
        self.cfg = cfg
        self.db = db
        self.session = session
        self.coins_func = coins_func

    def run(self) -> int:
        cmap = self.db.get_state("coingecko_map") or {}
        coins = self.coins_func()
        done = 0
        for sym in coins:
            info = cmap.get(sym)
            if not info or not info.get("id"):
                continue
            try:
                resp = fetch(self.session, CHART_URL.format(id=info["id"]), headers=headers(self.cfg), timeout=30)
                if resp.status_code == 429:
                    log.warning("CoinGecko-limiet bereikt bij historie; rest volgt bij de volgende ronde")
                    break
                resp.raise_for_status()
            except requests.RequestException as exc:
                log.info("Historie %s mislukt: %s", sym, exc)
                continue
            series = parse_chart(resp.json())
            self.db.executemany(
                "INSERT INTO prices(market, ticker, date, close) VALUES ('crypto', ?, ?, ?) "
                "ON CONFLICT(market, ticker, date) DO UPDATE SET close=excluded.close",
                [(sym, d, c) for d, c in series],
            )
            done += 1
        if coins and done == 0 and cmap:
            raise RuntimeError("Geen crypto-historie opgehaald (CoinGecko niet bereikbaar?)")
        return done
