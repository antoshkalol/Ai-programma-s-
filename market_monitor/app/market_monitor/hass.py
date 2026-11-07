"""Koppeling met Home Assistant: sensoren, events en pushberichten.

Binnen een add-on is de Supervisor bereikbaar op http://supervisor/core/api met het token in
de omgevingsvariabele SUPERVISOR_TOKEN (vereist 'homeassistant_api: true' in config.yaml).

Sensoren (per markt: stocks / crypto):
  sensor.market_monitor_<markt>_top_pick        - hoogste koopscore
  sensor.market_monitor_<markt>_summary         - kop van de laatste samenvatting (details in attributen)
  sensor.market_monitor_<markt>_last_big_trade  - laatste grote transactie
  sensor.market_monitor_alert                   - laatste noodmelding
  sensor.market_monitor_status                  - ok / fout
Events:
  market_monitor_big_trade, market_monitor_summary, market_monitor_alert
Pushberichten (als 'notify_service' is ingevuld):
  * elk uur de samenvatting van beide markten (normaal)
  * noodmeldingen direct, met hoge prioriteit (optioneel 'critical': doorbreekt stil/niet storen)
"""

from __future__ import annotations

import json
import logging
import os

import requests

from .config import MARKET_LABELS
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

    def _post(self, path: str, payload: dict) -> bool:
        try:
            r = self.session.post(
                f"{API}{path}",
                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
                data=json.dumps(payload, ensure_ascii=False, default=str),
                timeout=10,
            )
            if r.status_code >= 400:
                log.warning("Home Assistant %s gaf %s: %s", path, r.status_code, r.text[:200])
                return False
            return True
        except requests.RequestException as exc:
            log.warning("Home Assistant niet bereikbaar: %s", exc)
            return False

    def set_state(self, entity: str, state, attributes: dict) -> None:
        self._post(f"/states/{entity}", {"state": str(state)[:250], "attributes": attributes})

    def fire(self, event: str, data: dict) -> None:
        self._post(f"/events/{event}", data)

    def call_service(self, domain: str, service: str, data: dict) -> bool:
        return self._post(f"/services/{domain}/{service}", data)

    def notify(self, title: str, message: str, urgent: bool = False) -> None:
        if not self.cfg.notify_service:
            return
        data: dict = {"tag": "market_monitor_alert" if urgent else "market_monitor_summary"}
        if urgent:
            data.update({"ttl": 0, "priority": "high"})
            if self.cfg.alert_critical:
                # iOS: critical alert (ook bij stil/niet storen); Android: alarmkanaal
                data["push"] = {"sound": {"name": "default", "critical": 1, "volume": 1.0}}
                data["channel"] = "alarm_stream"
            else:
                data["push"] = {"interruption-level": "time-sensitive"}
        self.call_service("notify", self.cfg.notify_service, {"title": title, "message": message, "data": data})

    # ------------------------------------------------------------------ publicatie
    def publish_scores(self, market: str) -> None:
        if not self.enabled:
            return
        rows = self.db.query("SELECT * FROM scores_latest WHERE market=? ORDER BY score DESC LIMIT 5", (market,))
        if not rows:
            return
        top = rows[0]
        self.set_state(
            f"sensor.market_monitor_{market}_top_pick",
            top["ticker"],
            {
                "friendly_name": f"Market Monitor {MARKET_LABELS[market].lower()}: beste kans",
                "icon": "mdi:bitcoin" if market == "crypto" else "mdi:trending-up",
                "markt": MARKET_LABELS[market],
                "score": top["score"],
                "kans_omhoog": round((top["prob_up"] or 0) * 100),
                "instapmoment": top["timing"],
                "koers": top["price"],
                "redenen": json.loads(top["reasons"] or "[]"),
                "top5": [{"ticker": r["ticker"], "score": r["score"], "kans_omhoog": round((r["prob_up"] or 0) * 100),
                          "instapmoment": r["timing"]} for r in rows],
            },
        )

    def publish_summaries(self, markets: list[str]) -> None:
        """Werkt de sensoren bij en stuurt (optioneel) één pushbericht met beide markten."""
        lines = []
        for market in markets:
            s = latest_summary(self.db, market)
            if not s:
                continue
            c = s["content"]
            text = "\n".join(f"- {p}" for p in c.get("key_points", []))
            top = self.db.query_one("SELECT ticker, score FROM scores_latest WHERE market=? ORDER BY score DESC LIMIT 1",
                                    (market,))
            lines.append(f"{MARKET_LABELS[market]}: {c.get('headline', '')}"
                         + (f" (beste kans: {top['ticker']}, score {top['score']:.0f})" if top else ""))
            if not self.enabled:
                continue
            self.set_state(
                f"sensor.market_monitor_{market}_summary",
                c.get("headline", ""),
                {
                    "friendly_name": f"Market Monitor {MARKET_LABELS[market].lower()}: samenvatting",
                    "icon": "mdi:newspaper-variant-outline",
                    "markt": MARKET_LABELS[market],
                    "stemming": c.get("market_mood"),
                    "kernpunten": c.get("key_points", []),
                    "transacties": c.get("notable_trades", []),
                    "koopvisie": c.get("buy_view"),
                    "let_op": c.get("watch_out", []),
                    "tekst": text,
                    "methode": s["method"],
                    "periode_van": s["period_start"],
                    "periode_tot": s["period_end"],
                },
            )
            self.fire("market_monitor_summary", {"market": market, "headline": c.get("headline"),
                                                 "mood": c.get("market_mood"), "text": text,
                                                 "buy_view": c.get("buy_view")})
        if self.enabled and lines and self.cfg.notify_summaries:
            self.notify("Markt-update", "\n".join(lines))

    def publish_new_trades(self, trades: list[dict]) -> None:
        if not self.enabled or not trades:
            return
        for market in ("stocks", "crypto"):
            big = [t for t in trades if t.get("market", "stocks") == market and t.get("tx_type") in ("buy", "sell")
                   and (t.get("value_usd") or 0) >= self.cfg.big_trade_alert_usd]
            for t in big[:10]:
                self.fire(
                    "market_monitor_big_trade",
                    {
                        "market": market,
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
                    f"sensor.market_monitor_{market}_last_big_trade",
                    describe_trade(t),
                    {"friendly_name": f"Market Monitor {MARKET_LABELS[market].lower()}: grote transactie",
                     "icon": "mdi:cash-multiple",
                     **{k: t.get(k) for k in ("actor_name", "actor_role", "ticker", "company", "tx_type",
                                              "value_usd", "tx_date", "filed_at", "url")}},
                )

    def push_alert(self, alert: dict) -> None:
        """Noodmelding: direct naar telefoon (notify), HA-melding, event en sensor."""
        label = MARKET_LABELS.get(alert["market"], alert["market"])
        title = f"Noodmelding {label.lower()}: {alert['title']}"
        message = alert.get("message") or ""
        if alert.get("ai_note"):
            message += f"\n\nAI: {alert['ai_note']}"
        message += "\n\nGeen beleggingsadvies; een signaal is geen garantie."
        if not self.enabled:
            return
        self.notify(title, message, urgent=True)
        self.call_service("persistent_notification", "create",
                          {"title": title, "message": message, "notification_id": f"market_monitor_alert_{alert['id']}"})
        payload = {k: alert.get(k) for k in ("id", "market", "asset", "kind", "title", "message", "strength",
                                             "ai_note", "created_at")}
        self.fire("market_monitor_alert", payload)
        self.set_state("sensor.market_monitor_alert", alert["title"],
                       {"friendly_name": "Market Monitor noodmelding", "icon": "mdi:alarm-light", **payload})

    def publish_status(self, ok: bool, details: dict) -> None:
        if not self.enabled:
            return
        self.set_state("sensor.market_monitor_status", "ok" if ok else "fout",
                       {"friendly_name": "Market Monitor status", "icon": "mdi:heart-pulse", **details})
