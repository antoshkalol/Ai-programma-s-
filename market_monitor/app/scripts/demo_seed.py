"""Vult een lokale database met FICTIEVE demodata om het dashboard te bekijken zonder internet.

Alle namen, tickers en koppen zijn verzonnen. Gebruik:
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

from market_monitor.config import load_config  # noqa: E402
from market_monitor.db import Database, iso, utcnow  # noqa: E402
from market_monitor.filtering import NewsIngestor  # noqa: E402
from market_monitor.scoring import ScoringEngine  # noqa: E402
from market_monitor.summary import Summarizer  # noqa: E402
from market_monitor.tickers import TickerExtractor  # noqa: E402

TICKERS = {
    "ALFA": ("Alfa Robotics (demo)", 48.0, 0.004),
    "BETA": ("Beta Chips (demo)", 120.0, 0.002),
    "GAMA": ("Gamma Energy (demo)", 35.0, -0.003),
    "DELT": ("Delta Health (demo)", 82.0, 0.001),
    "EPSI": ("Epsilon Bank (demo)", 61.0, 0.0),
    "ZETA": ("Zeta Software (demo)", 210.0, -0.001),
}


def main(data_dir: str) -> None:
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "options.json").write_text(json.dumps({
        "watchlist": list(TICKERS),
        "trusted_domains": ["demo-beursnieuws.example", "demo-financial.example"],
        "enable_sec_form4": False, "enable_house": False, "enable_senate": False,
    }))
    cfg = load_config(str(d))
    db_path = cfg.db_path
    if db_path.exists():
        db_path.unlink()
    db = Database(db_path)
    now = utcnow()
    rnd = random.Random(7)

    # Koersen (synthetisch)
    for t, (name, start, drift) in TICKERS.items():
        price, rows = start, []
        for i in range(130):
            day = (now - timedelta(days=130 - i)).strftime("%Y-%m-%d")
            price *= 1 + drift + rnd.gauss(0, 0.014) + (0.01 * math.sin(i / 9) if t == "ALFA" else 0)
            if t == "ALFA" and i > 124:
                price *= 0.992  # kleine terugval: 'gunstig instapmoment'
            rows.append((t, day, round(price, 2)))
        db.executemany("INSERT INTO prices(ticker, date, close) VALUES (?,?,?)", rows)
        db.execute("INSERT INTO price_meta(ticker, name, currency, last_price, prev_close, fetched_at) VALUES (?,?,?,?,?,?)",
                   (t, name, "USD", rows[-1][2], rows[-2][2], iso(now)))

    def ago(**kw):
        return iso(now - timedelta(**kw))

    trades = [
        ("i1", "sec_form4", "Demo Persoon A", "Chief Executive Officer", "insider", None, "ALFA", "buy", 2_450_000, None, None, 3, 1),
        ("i2", "sec_form4", "Demo Persoon B", "Director", "insider", None, "ALFA", "buy", 640_000, None, None, 4, 2),
        ("i3", "sec_form4", "Demo Persoon C", "CFO", "insider", None, "BETA", "buy", 310_000, None, None, 2, 0),
        ("i4", "sec_form4", "Demo Fonds D", "10%-aandeelhouder", "insider", None, "ZETA", "sell", 18_500_000, None, None, 2, 0),
        ("i5", "sec_form4", "Demo Persoon E", "Chief Operating Officer", "insider", None, "GAMA", "sell", 1_200_000, None, None, 3, 1),
        ("p1", "senate", "Demo Senator F", "Senator", "politician", "D", "ALFA", "buy", 75_000.5, 50_001, 100_000, 30, 1),
        ("p2", "house", "Demo Afgevaardigde G", "Afgevaardigde (XX01)", "politician", "R", "BETA", "buy", 375_000.5, 250_001, 500_000, 25, 0),
        ("p3", "house", "Demo Afgevaardigde H", "Afgevaardigde (XX02)", "politician", "D", "DELT", "sell", 32_500.5, 15_001, 50_000, 41, 2),
        ("p4", "senate", "Demo Senator I", "Senator", "politician", "R", "EPSI", "buy", 3_000_000.5, 1_000_001, 5_000_000, 12, 0),
    ]
    rows = []
    for (tid, src, name, role, typ, party, tk, tx, val, lo, hi, delay, filed_days) in trades:
        filed = now - timedelta(days=filed_days, hours=rnd.randint(1, 20))
        rows.append({
            "id": tid, "source": src, "actor_name": name, "actor_role": role, "actor_type": typ, "party": party,
            "ticker": tk, "company": TICKERS[tk][0], "asset_type": "aandeel", "tx_type": tx,
            "value_usd": val, "value_low": lo or val, "value_high": hi or val,
            "tx_date": (filed - timedelta(days=delay)).strftime("%Y-%m-%d"), "filed_at": iso(filed),
            "url": "https://example.com/demo", "planned": 1 if tid == "i4" else 0,
            "note": "10b5-1 handelsplan (vooraf ingepland)" if tid == "i4" else None,
            "created_at": iso(filed if filed_days else now - timedelta(minutes=rnd.randint(2, 25))),
        })
    db.insert_trades(rows)

    ing = NewsIngestor(cfg, db, TickerExtractor(db, cfg.watchlist))
    headlines = [
        ("Alfa Robotics (demo) wins large factory automation contract, shares rise", "demo-financial.example", 6),
        ("Alfa Robotics (demo) stock rises after large automation contract win", "demo-beursnieuws.example", 9),
        ("Beta Chips (demo) beats estimates and raises guidance on strong demand", "demo-financial.example", 14),
        ("Zeta Software (demo) faces probe over accounting practices", "demo-financial.example", 18),
        ("Centrale bank (demo) houdt rente stabiel, markt reageert kalm", "demo-beursnieuws.example", 22),
        ("Gamma Energy (demo) daalt na winstwaarschuwing", "demo-beursnieuws.example", 27),
        ("Epsilon Bank (demo) announces buyback programme", "demo-financial.example", 40),
        ("This DEMO PENNY STOCK Could Make You A MILLIONAIRE Before It's Too Late!", "hype-demo.example", 8),
        ("Is Delta Health (demo) the next 100x?! Buy now!", "hype-demo.example", 12),
        ("Rumour mill: Delta Health (demo) takeover talk", "anonieme-blog.example", 15),
    ]
    items = []
    for i, (title, dom, mins) in enumerate(headlines):
        items.append({"id": f"n{i}", "source": dom.split(".")[0], "domain": dom, "kind": "news", "title": title,
                      "summary": "", "url": "https://example.com/demo", "published_at": ago(minutes=mins)})
    items.append({"id": "x1", "source": "X @demo_account", "domain": "x.com", "kind": "post", "author": "demo_account",
                  "title": "Demo post: we zien sterke vraag naar robotica dit kwartaal.", "summary": "",
                  "url": "https://example.com/demo", "published_at": ago(minutes=11)})
    ing(items, trusted_authors={"demo_account"})

    ScoringEngine(cfg, db).run()
    Summarizer(cfg, db, ing).run()
    print(f"Demo-database klaar in {db_path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "./devdata")
