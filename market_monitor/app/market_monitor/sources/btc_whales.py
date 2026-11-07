"""Grote bitcoin-transfers ('walvissen') uit de nieuwste blokken, via blockchain.info (gratis).

Let op: een grote transfer zegt niet wie koopt of verkoopt. Vaak zijn het interne verschuivingen van
beurzen. Daarom tonen we ze wel, maar tellen ze niet mee in de koopscore.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from ..db import Database, iso
from ..net import fetch
from ..scoring import fmt_num

log = logging.getLogger(__name__)

LATEST = "https://blockchain.info/latestblock"
RAWBLOCK = "https://blockchain.info/rawblock/{hash}"
MAX_BLOCKS_PER_RUN = 3


def short_addr(addr: str | None) -> str:
    if not addr:
        return "onbekend"
    return addr if len(addr) <= 14 else f"{addr[:6]}...{addr[-4:]}"


def whale_transfers(block: dict, min_btc: float) -> list[dict]:
    """Transacties waarbij minstens min_btc naar andere adressen gaat dan de afzenders (wisselgeld telt niet)."""
    out = []
    ts = block.get("time")
    for tx in block.get("tx", []):
        inputs = tx.get("inputs") or []
        if not inputs or "prev_out" not in inputs[0]:
            continue  # coinbase (nieuw gemunte bitcoin)
        in_addrs = {i["prev_out"].get("addr") for i in inputs if i.get("prev_out")}
        moved = sum(o.get("value", 0) for o in tx.get("out", []) if o.get("addr") not in in_addrs) / 1e8
        if moved < min_btc:
            continue
        biggest = max((o for o in tx.get("out", []) if o.get("addr") not in in_addrs),
                      key=lambda o: o.get("value", 0), default={})
        out.append(
            {
                "hash": tx.get("hash"),
                "btc": round(moved, 4),
                "from": short_addr(next(iter(a for a in in_addrs if a), None)),
                "to": short_addr(biggest.get("addr")),
                "n_in": len(inputs),
                "n_out": len(tx.get("out", [])),
                "time": tx.get("time") or ts,
            }
        )
    return out


class BtcWhaleSource:
    name = "btc_whales"

    def __init__(self, cfg, db: Database, session: requests.Session):
        self.cfg = cfg
        self.db = db
        self.session = session

    def btc_price(self) -> float | None:
        row = self.db.query_one("SELECT last_price FROM price_meta WHERE market='crypto' AND ticker='BTC'")
        return row["last_price"] if row and row["last_price"] else None

    def run(self) -> int:
        price = self.btc_price()
        if not price:
            log.info("Nog geen BTC-koers bekend; walvissen volgen na de eerste koersronde")
            return 0
        min_btc = self.cfg.crypto_whale_min_usd / price
        latest = fetch(self.session, LATEST, timeout=20)
        latest.raise_for_status()
        info = latest.json()
        last_height = self.db.get_state("btc_last_height")
        if last_height is not None and info["height"] <= last_height:
            return 0

        blocks, h, bhash = [], info["height"], info["hash"]
        while bhash and len(blocks) < MAX_BLOCKS_PER_RUN and (last_height is None or h > last_height):
            resp = fetch(self.session, RAWBLOCK.format(hash=bhash), timeout=60)
            resp.raise_for_status()
            block = resp.json()
            blocks.append(block)
            bhash, h = block.get("prev_block"), h - 1
            if last_height is None:
                break  # eerste keer: alleen het nieuwste blok

        trades = []
        for block in blocks:
            for w in whale_transfers(block, min_btc):
                when = datetime.fromtimestamp(w["time"], tz=timezone.utc) if w["time"] else None
                trades.append(
                    {
                        "id": f"btc:{w['hash']}",
                        "market": "crypto",
                        "source": "btc_chain",
                        "actor_name": f"Wallet {w['from']}",
                        "actor_role": "Bitcoin-walvis",
                        "actor_type": "whale",
                        "ticker": "BTC",
                        "company": f"naar {w['to']}",
                        "asset_type": "on-chain transfer",
                        "tx_type": "other",
                        "tx_code": "transfer",
                        "shares": w["btc"],
                        "price": price,
                        "value_usd": round(w["btc"] * price, 2),
                        "value_low": round(w["btc"] * price, 2),
                        "value_high": round(w["btc"] * price, 2),
                        "tx_date": when.strftime("%Y-%m-%d") if when else None,
                        "filed_at": iso(when) if when else None,
                        "url": f"https://mempool.space/tx/{w['hash']}",
                        "note": f"{fmt_num(w['btc'])} BTC verplaatst; richting onbekend (vaak beurs-intern)",
                    }
                )
        self.db.set_state("btc_last_height", info["height"])
        return len(self.db.insert_trades(trades))
