"""Vult een lokale database met FICTIEVE demodata om het dashboard te bekijken zonder internet.

Alle namen, tickers, munten en koppen zijn verzonnen. Gebruik:
    python scripts/demo_seed.py ./devdata
    python -m market_monitor --data-dir ./devdata --port 8099 --no-scheduler
"""

from __future__ import annotations

import json
import math
import random
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_monitor.alerts import AlertEngine  # noqa: E402
from market_monitor.config import load_config  # noqa: E402
from market_monitor.db import Database, iso, utcnow  # noqa: E402
from market_monitor.filtering import NewsIngestor  # noqa: E402
from market_monitor.recommend import Recommender  # noqa: E402
from market_monitor.scoring import CryptoScoringEngine, ScoringEngine  # noqa: E402
from market_monitor.summary import Summarizer  # noqa: E402
from market_monitor.tickers import TickerExtractor  # noqa: E402

STOCKS = {
    "ALFA": ("Alfa Robotics (demo)", 48.0, 0.004),
    "BETA": ("Beta Chips (demo)", 120.0, 0.002),
    "GAMA": ("Gamma Energy (demo)", 35.0, -0.003),
    "DELT": ("Delta Health (demo)", 82.0, 0.001),
    "EPSI": ("Epsilon Bank (demo)", 61.0, 0.0),
    "ZETA": ("Zeta Software (demo)", 210.0, -0.001),
}
COINS = {  # symbool: (naam, startkoers, drift, rang)
    "ALPC": ("Alphacoin (demo)", 52000.0, 0.003, 1),
    "BETC": ("Betacoin (demo)", 2400.0, 0.002, 2),
    "GAMC": ("Gammacoin (demo)", 140.0, 0.006, 6),
    "DELC": ("Deltacoin (demo)", 0.42, -0.004, 12),
    "EPSC": ("Epsiloncoin (demo)", 0.0000182, 0.001, 25),
}


def series(rnd, start, drift, n=130, wiggle=0.0, pullback=False, vol=0.014):
    price, out = start, []
    for i in range(n):
        price *= 1 + drift + rnd.gauss(0, vol) + (wiggle * math.sin(i / 9))
        if pullback and i > n - 6:
            price *= 0.992
        out.append(price)
    return out


def main(data_dir: str) -> None:
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "options.json").write_text(json.dumps({
        "stock_watchlist": list(STOCKS),
        "crypto_watchlist": list(COINS),
        "trusted_domains": ["demo-beursnieuws.example", "demo-financial.example", "demo-cryptonieuws.example",
                            "demo-ledger.example", "demo-blockdaily.example"],
        "enable_sec_form4": False, "enable_house": False, "enable_senate": False,
    }))
    cfg = load_config(str(d))
    db_path = cfg.db_path
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
    db = Database(db_path)
    now = utcnow()
    rnd = random.Random(7)

    def ago(**kw):
        return iso(now - timedelta(**kw))

    # ------------------------------------------------------------ koersen aandelen
    for t, (name, start, drift) in STOCKS.items():
        closes = series(rnd, start, drift, wiggle=0.01 if t == "ALFA" else 0, pullback=t == "ALFA")
        rows = [("stocks", t, (now - timedelta(days=130 - i)).strftime("%Y-%m-%d"), round(c, 2))
                for i, c in enumerate(closes)]
        db.executemany("INSERT INTO prices(market, ticker, date, close) VALUES (?,?,?,?)", rows)
        chg = (closes[-1] / closes[-2] - 1) * 100
        db.execute("INSERT INTO price_meta(market, ticker, name, currency, last_price, prev_close, chg_24h, fetched_at)"
                   " VALUES ('stocks',?,?,?,?,?,?,?)", (t, name, "USD", closes[-1], closes[-2], chg, iso(now)))

    # ------------------------------------------------------------ koersen crypto + snapshots
    cmap = {}
    for sym, (name, start, drift, rank) in COINS.items():
        closes = series(rnd, start, drift, n=180, vol=0.03, pullback=sym == "BETC")
        rows = [("crypto", sym, (now - timedelta(days=180 - i)).strftime("%Y-%m-%d"), c) for i, c in enumerate(closes)]
        db.executemany("INSERT INTO prices(market, ticker, date, close) VALUES (?,?,?,?)", rows)
        price = closes[-1]
        chg1h = 9.4 if sym == "GAMC" else rnd.uniform(-1.2, 1.5)
        chg24 = (closes[-1] / closes[-2] - 1) * 100 + (11 if sym == "GAMC" else 0)
        chg7 = (closes[-1] / closes[-8] - 1) * 100
        vol_now = start * 1e6 * (1.12 if sym == "GAMC" else 1.0)
        db.execute(
            "INSERT INTO price_meta(market, ticker, name, currency, last_price, prev_close, chg_1h, chg_24h, chg_7d,"
            " volume, market_cap, rank, fetched_at) VALUES ('crypto',?,?,'USD',?,?,?,?,?,?,?,?,?)",
            (sym, name, price, price / (1 + chg24 / 100), chg1h, chg24, chg7, vol_now, price * 1e7, rank, iso(now)),
        )
        for mins, vol in ((62, start * 1e6), (1, vol_now)):
            db.execute(
                "INSERT INTO crypto_snapshots(coin, ts, price, volume, chg_1h, chg_24h, chg_7d, market_cap, rank)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (sym, ago(minutes=mins), price, vol, chg1h if mins == 1 else 0.2, chg24, chg7, price * 1e7, rank),
            )
        cmap[sym] = {"id": name.lower().replace(" ", "-"), "name": name.replace(" (demo)", ""), "rank": rank}
    db.set_state("coingecko_map", cmap)

    # ------------------------------------------------------------ transacties
    trades = [
        # (id, markt, bron, naam, rol, type, partij, ticker, actie, waarde, laag, hoog, vertraging, dagen geleden)
        ("i1", "stocks", "sec_form4", "Demo Persoon A", "Chief Executive Officer", "insider", None, "ALFA", "buy", 2_450_000, None, None, 3, 1),
        ("i2", "stocks", "sec_form4", "Demo Persoon B", "Director", "insider", None, "ALFA", "buy", 640_000, None, None, 4, 2),
        ("i3", "stocks", "sec_form4", "Demo Persoon C", "CFO", "insider", None, "BETA", "buy", 310_000, None, None, 2, 0),
        ("i4", "stocks", "sec_form4", "Demo Fonds D", "10%-aandeelhouder", "insider", None, "ZETA", "sell", 18_500_000, None, None, 2, 0),
        ("i5", "stocks", "sec_form4", "Demo Persoon E", "Chief Operating Officer", "insider", None, "GAMA", "sell", 1_200_000, None, None, 3, 1),
        ("i6", "stocks", "sec_form4", "Demo Persoon J", "Director", "insider", None, "ALFA", "buy", 420_000, None, None, 2, 0),
        ("p1", "stocks", "senate", "Demo Senator F", "Senator", "politician", "D", "ALFA", "buy", 75_000.5, 50_001, 100_000, 30, 1),
        ("p2", "stocks", "house", "Demo Afgevaardigde G", "Afgevaardigde (XX01)", "politician", "R", "BETA", "buy", 375_000.5, 250_001, 500_000, 25, 0),
        ("p3", "stocks", "house", "Demo Afgevaardigde H", "Afgevaardigde (XX02)", "politician", "D", "DELT", "sell", 32_500.5, 15_001, 50_000, 41, 2),
        ("p4", "stocks", "senate", "Demo Senator I", "Senator", "politician", "R", "EPSI", "buy", 3_000_000.5, 1_000_001, 5_000_000, 12, 0),
        ("c1", "crypto", "sec_8k", "Demo Treasury Corp", "Bedrijf", "company", None, "ALPC", "buy", 365_000_000, None, None, 0, 0),
        ("c2", "crypto", "sec_8k", "Voorbeeld Mining Inc", "Bedrijf", "company", None, "BETC", "buy", 48_000_000, None, None, 0, 3),
        ("c3", "crypto", "sec_8k", "Demo Holdings Ltd", "Bedrijf", "company", None, "ALPC", "sell", 22_000_000, None, None, 0, 6),
    ]
    rows = []
    for (tid, market, src, name, role, typ, party, tk, tx, val, lo, hi, delay, filed_days) in trades:
        filed = now - timedelta(days=filed_days, hours=rnd.randint(1, 20))
        company = STOCKS[tk][0] if market == "stocks" else {"c1": "DTC", "c2": "VMI", "c3": "DHL"}[tid]
        coin_price = COINS[tk][1] if market == "crypto" else None
        rows.append({
            "id": tid, "market": market, "source": src, "actor_name": name, "actor_role": role, "actor_type": typ,
            "party": party, "ticker": tk, "company": company, "asset_type": "aandeel" if market == "stocks" else "crypto",
            "tx_type": tx, "value_usd": val, "value_low": lo or val, "value_high": hi or val,
            "shares": round(val / coin_price) if coin_price else None,
            "tx_date": (filed - timedelta(days=delay)).strftime("%Y-%m-%d"), "filed_at": iso(filed),
            "url": "https://example.com/demo", "planned": 1 if tid == "i4" else 0,
            "note": "10b5-1 handelsplan (vooraf ingepland)" if tid == "i4" else None,
            "created_at": iso(filed if filed_days else now - timedelta(minutes=rnd.randint(2, 25))),
        })
    # walvis-transfer
    rows.append({
        "id": "w1", "market": "crypto", "source": "btc_chain", "actor_name": "Wallet bc1qde...mo01",
        "actor_role": "Walvis", "actor_type": "whale", "ticker": "ALPC", "company": "naar 3Demo...x9Kq",
        "asset_type": "on-chain transfer", "tx_type": "other", "shares": 1850, "price": COINS["ALPC"][1],
        "value_usd": 1850 * COINS["ALPC"][1], "value_low": 1850 * COINS["ALPC"][1], "value_high": 1850 * COINS["ALPC"][1],
        "tx_date": now.strftime("%Y-%m-%d"), "filed_at": ago(minutes=40), "url": "https://example.com/demo",
        "note": "1.850 ALPC verplaatst; richting onbekend (vaak beurs-intern)", "created_at": ago(minutes=38),
    })
    db.insert_trades(rows)

    # ------------------------------------------------------------ nieuws
    extractors = {"stocks": TickerExtractor(db, cfg.stock_watchlist, "stocks"),
                  "crypto": TickerExtractor(db, cfg.crypto_watchlist, "crypto")}
    ing = NewsIngestor(cfg, db, extractors)
    stock_news = [
        ("Alfa Robotics (demo) wins large factory automation contract, shares rise", "demo-financial.example", 6),
        ("Alfa Robotics (demo) stock rises after large automation contract win", "demo-beursnieuws.example", 9),
        ("Beta Chips (demo) beats estimates and raises guidance on strong demand", "demo-financial.example", 14),
        ("Zeta Software (demo) faces probe over accounting practices", "demo-financial.example", 18),
        ("Centrale bank (demo) houdt rente stabiel, markt reageert kalm", "demo-beursnieuws.example", 22),
        ("Gamma Energy (demo) daalt na winstwaarschuwing", "demo-beursnieuws.example", 27),
        ("Epsilon Bank (demo) announces buyback programme", "demo-financial.example", 40),
        ("This DEMO PENNY STOCK Could Make You A MILLIONAIRE Before It's Too Late!", "hype-demo.example", 8),
        ("Is Delta Health (demo) the next 100x?! Buy now!", "hype-demo.example", 12),
    ]
    crypto_news = [
        ("Regulator (demo) approves first Gammacoin ETF, Gammacoin surges", "demo-cryptonieuws.example", 7),
        ("Gammacoin surges as regulator (demo) approves first Gammacoin ETF", "demo-ledger.example", 9),
        ("First Gammacoin ETF approved by regulator (demo); Gammacoin surges", "demo-blockdaily.example", 12),
        ("Demo Treasury Corp buys more Alphacoin for its balance sheet", "demo-cryptonieuws.example", 20),
        ("Deltacoin network suffers outage, price drops", "demo-ledger.example", 31),
        ("Betacoin developers schedule major upgrade", "demo-blockdaily.example", 44),
        ("Best crypto to buy now: this presale could be the next 1000x gem!", "hype-crypto.example", 10),
        ("Epsiloncoin price prediction: could hit $1 soon?", "hype-crypto.example", 16),
    ]
    for market, items in (("stocks", stock_news), ("crypto", crypto_news)):
        ing([{"id": f"n{i}", "source": dom.split(".")[0], "domain": dom, "kind": "news", "title": title,
              "summary": "", "url": "https://example.com/demo", "published_at": ago(minutes=mins)}
             for i, (title, dom, mins) in enumerate(items)], market=market)
    ing([{"id": "x1", "source": "X @demo_account", "domain": "x.com", "kind": "post", "author": "demo_account",
          "title": "Demo post: we zien sterke vraag naar robotica dit kwartaal.", "summary": "",
          "url": "https://example.com/demo", "published_at": ago(minutes=11)}],
        market="stocks", trusted_authors={"demo_account"})

    ScoringEngine(cfg, db).run()
    CryptoScoringEngine(cfg, db).run()
    rec = Recommender(cfg, db)
    rec.update("stocks")
    rec.update("crypto")
    Summarizer(cfg, db, ing).run()
    sent = AlertEngine(cfg, db).run()
    print(f"Demo-database klaar in {db_path} ({sent} noodmeldingen)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "./devdata")
