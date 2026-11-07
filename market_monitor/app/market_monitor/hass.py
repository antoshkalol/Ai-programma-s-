"""Koppeling met Home Assistant: sensoren bijwerken en events afvuren voor automatiseringen.

Binnen een add-on is de Supervisor bereikbaar op http://supervisor/core/api met het token in
de omgevingsvariabele SUPERVISOR_TOKEN (vereist 'homeassistant_api: true' in config.yaml).

Sensoren:
  sensor.market_monitor_top_pick       - aandeel met de hoogste score
  sensor.market_monitor_summary        - kop van de laatste samenvatting (volledige tekst in attributen)
  sensor.market_monitor_last_big_trade - laatste grote transactie
  sensor.market_monitor_status         - ok / fout
Events:
  market_monitor_big_trade             - bij elke nieuwe transactie boven de alert-drempel
  market_monitor_summary               - bij elke nieuwe samenvatting
"""

from __future__ import annotations

import json
import logging
import os

import requests

from .db import Database
from .scoring import fmt_usd
from .summary import describe_trade, latest_summary

log = logging.getLogger(__name__)

API = "http://supervisor/core/api"


class HomeAssistant:
    def __init__(self, cfg, db: Database):
        self.cfg = cfg
        self.db = db
        self.token = os.environ.get("SUPERVISOR_TOKEN")
        self.session = requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(self.token) and self.cfg.publish_to_home_assistant

    def _post(self, path: str, payload: dict) -> None:
        try:
            r = self.session.post(
                f"{API}{path}",
                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
                data=json.dumps(payload, ensure_ascii=False, default=str),
                timeout=10,
            )
            if r.status_code >= 400:
                log.warning("Home Assistant %s gaf %s: %s", path, r.status_code, r.text[:200])
        except requests.RequestException as exc:
            log.warning("Home Assistant niet bereikbaar: %s", exc)

    def set_state(self, entity: str, state, attributes: dict) -> None:
        self._post(f"/states/{entity}", {"state": str(state)[:250], "attributes": attributes})

    def fire(self, event: str, data: dict) -> None:
        self._post(f"/events/{event}", data)

    # ------------------------------------------------------------------ publicatie
    def publish_scores(self) -> None:
        if not self.enabled:
            return
        rows = self.db.query("SELECT * FROM scores_latest ORDER BY score DESC LIMIT 5")
        if not rows:
            return
        top = rows[0]
        self.set_state(
            "sensor.market_monitor_top_pick",
            top["ticker"],
            {
                "friendly_name": "Market Monitor beste kans",
                "icon": "mdi:trending-up",
                "score": top["score"],
                "kans_omhoog": round((top["prob_up"] or 0) * 100),
                "instapmoment": top["timing"],
                "koers": top["price"],
                "redenen": json.loads(top["reasons"] or "[]"),
                "top5": [{"ticker": r["ticker"], "score": r["score"], "kans_omhoog": round((r["prob_up"] or 0) * 100),
                          "instapmoment": r["timing"]} for r in rows],
            },
        )

    def publish_summary(self) -> None:
        if not self.enabled:
            return
        s = latest_summary(self.db)
        if not s:
            return
        c = s["content"]
        text = "\n".join(f"- {p}" for p in c.get("key_points", []))
        attrs = {
            "friendly_name": "Market Monitor samenvatting",
            "icon": "mdi:newspaper-variant-outline",
            "stemming": c.get("market_mood"),
            "kernpunten": c.get("key_points", []),
            "transacties": c.get("notable_trades", []),
            "koopvisie": c.get("buy_view"),
            "let_op": c.get("watch_out", []),
            "tekst": text,
            "methode": s["method"],
            "periode_van": s["period_start"],
            "periode_tot": s["period_end"],
        }
        self.set_state("sensor.market_monitor_summary", c.get("headline", ""), attrs)
        self.fire("market_monitor_summary", {"headline": c.get("headline"), "mood": c.get("market_mood"),
                                             "text": text, "buy_view": c.get("buy_view")})

    def publish_new_trades(self, trades: list[dict]) -> None:
        if not self.enabled or not trades:
            return
        big = [t for t in trades if t.get("tx_type") in ("buy", "sell")
               and (t.get("value_usd") or 0) >= self.cfg.big_trade_alert_usd]
        for t in big[:10]:
            self.fire(
                "market_monitor_big_trade",
                {
                    "who": t["actor_name"],
                    "role": t.get("actor_role"),
                    "type": t["tx_type"],
                    "ticker": t.get("ticker"),
                    "company": t.get("company"),
                    "value": t.get("value_usd"),
                    "value_text": fmt_usd(t.get("value_usd")),
                    "description": describe_trade(t),
                    "url": t.get("url"),
                },
            )
        if big:
            t = max(big, key=lambda x: x.get("value_usd") or 0)
            self.set_state(
                "sensor.market_monitor_last_big_trade",
                describe_trade(t),
                {"friendly_name": "Market Monitor grote transactie", "icon": "mdi:cash-multiple", **{
                    k: t.get(k) for k in ("actor_name", "actor_role", "ticker", "company", "tx_type", "value_usd",
                                          "tx_date", "filed_at", "url")}},
            )

    def publish_status(self, ok: bool, details: dict) -> None:
        if not self.enabled:
            return
        self.set_state("sensor.market_monitor_status", "ok" if ok else "fout",
                       {"friendly_name": "Market Monitor status", "icon": "mdi:heart-pulse", **details})
