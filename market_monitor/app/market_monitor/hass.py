"""Koppeling met Home Assistant: sensoren, events en pushberichten (popups) op je telefoon.

Binnen een add-on is de Supervisor bereikbaar op http://supervisor met het token in de omgevingsvariabele
SUPERVISOR_TOKEN (vereist 'homeassistant_api: true' in config.yaml).

Pushberichten gaan naar de Home Assistant-app op je telefoon(s):
  * 'notify_service' leeg  -> automatisch naar alle telefoons met de app (notify.mobile_app_*)
  * 'notify_service' gevuld -> alleen naar die service(s), komma-gescheiden
Elk bericht verschijnt als popup (iPhone: banner, Android: 'heads-up'); tikken opent het dashboard.
  * uurbericht : vervangt het vorige uurbericht (geen stapel), niet tijdens 'quiet_hours'
  * noodmelding: hoge prioriteit; met 'alert_critical' ook door stil/niet storen heen

Sensoren (per markt: stocks / crypto):
  sensor.market_monitor_<markt>_top_pick, _summary, _last_big_trade, _recommendation
  sensor.market_monitor_alert, sensor.market_monitor_status
Events: market_monitor_big_trade, market_monitor_summary, market_monitor_alert
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta

import requests

from .config import MARKET_LABELS
from .db import Database, iso, parse_iso, utcnow
from .scoring import fmt_price, fmt_usd
from .summary import describe_trade, latest_summary

log = logging.getLogger(__name__)

SUPERVISOR = "http://supervisor"
API = SUPERVISOR + "/core/api"
COLOR = "#2a78d6"


def in_quiet_hours(spec: str, hour: int) -> bool:
    """'23-7' betekent van 23:00 tot 07:00 geen uurberichten."""
    try:
        start, end = (int(x) % 24 for x in spec.replace(" ", "").split("-"))
    except (ValueError, AttributeError):
        return False
    if start == end:
        return False
    return start <= hour < end if start < end else (hour >= start or hour < end)


def local_hour() -> int:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(os.environ.get("TZ", "Europe/Amsterdam"))).hour
    except Exception:  # noqa: BLE001 - zonder tijdzonedata: systeemtijd
        return datetime.now().hour


def recommendation_line(rec: dict | None) -> str:
    if not rec:
        return ""
    if rec.get("direction") == "neutral":
        return f"Aanbeveling: afwachten ({rec['ticker']})"
    return (f"Aanbeveling: {rec['label']} {rec['ticker']} - instap {fmt_price(rec['entry_low'])}-"
            f"{fmt_price(rec['entry_high'])}, doel {fmt_price(rec['target1'])}, stop {fmt_price(rec['stop'])}")


class HomeAssistant:
    def __init__(self, cfg, db: Database):
        self.cfg = cfg
        self.db = db
        self.token = os.environ.get("SUPERVISOR_TOKEN")
        self.session = requests.Session()
        self._targets: tuple[float, list[str]] | None = None
        self._panel: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.token) and self.cfg.publish_to_home_assistant

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def _post(self, path: str, payload: dict) -> bool:
        try:
            r = self.session.post(f"{API}{path}", headers=self._headers(),
                                  data=json.dumps(payload, ensure_ascii=False, default=str), timeout=10)
            if r.status_code >= 400:
                log.warning("Home Assistant %s gaf %s: %s", path, r.status_code, r.text[:200])
                return False
            return True
        except requests.RequestException as exc:
            log.warning("Home Assistant niet bereikbaar: %s", exc)
            return False

    def _get(self, url: str):
        try:
            r = self.session.get(url, headers=self._headers(), timeout=10)
            return r.json() if r.status_code < 400 else None
        except (requests.RequestException, ValueError) as exc:
            log.warning("Home Assistant niet bereikbaar: %s", exc)
            return None

    def set_state(self, entity: str, state, attributes: dict) -> None:
        self._post(f"/states/{entity}", {"state": str(state)[:250], "attributes": attributes})

    def fire(self, event: str, data: dict) -> None:
        self._post(f"/events/{event}", data)

    def call_service(self, domain: str, service: str, data: dict) -> bool:
        return self._post(f"/services/{domain}/{service}", data)

    # ------------------------------------------------------------------ telefoons vinden
    def notify_targets(self) -> list[str]:
        if self.cfg.notify_service.strip():
            return [s.strip().removeprefix("notify.") for s in self.cfg.notify_service.split(",") if s.strip()]
        if self._targets and time.time() - self._targets[0] < 600:
            return self._targets[1]
        found: list[str] = []
        for d in self._get(f"{API}/services") or []:
            if d.get("domain") != "notify":
                continue
            services = d.get("services") or {}
            names = services.keys() if isinstance(services, dict) else services
            found = sorted(n for n in names if str(n).startswith("mobile_app_"))
        self._targets = (time.time(), found)
        if not found:
            log.warning("Geen telefoon gevonden: installeer de Home Assistant-app en log in, of vul notify_service in")
        return found

    def panel_url(self) -> str:
        """Pad naar het dashboard in Home Assistant (voor 'tik om te openen')."""
        if self._panel is None:
            slug = None
            info = self._get(f"{SUPERVISOR}/addons/self/info")
            if isinstance(info, dict):
                slug = (info.get("data") or {}).get("slug")
            if not slug and os.environ.get("HOSTNAME"):
                slug = os.environ["HOSTNAME"].replace("-", "_")
            self._panel = f"/hassio/ingress/{slug}" if slug else "/"
        return self._panel

    # ------------------------------------------------------------------ pushberichten
    def build_notify_data(self, kind: str, tag: str) -> dict:
        url = self.panel_url()
        data: dict = {
            "tag": tag,
            "group": "market-monitor-" + ("alert" if kind == "alert" else "update"),
            "url": url,                      # iPhone: tik opent het dashboard
            "clickAction": url,              # Android: tik opent het dashboard
            "actions": [{"action": "URI", "title": "Open dashboard", "uri": url}],
            "notification_icon": "mdi:finance",
            "color": COLOR,
            # Popup: Android heads-up via kanaal met hoge prioriteit, iPhone banner
            "channel": "Market Monitor",
            "importance": "high",
            "priority": "high",
            "ttl": 0,
            "push": {"interruption-level": "active"},
        }
        if kind == "alert":
            data["channel"] = "Market Monitor noodmelding"
            data["push"] = {"interruption-level": "time-sensitive"}
            if self.cfg.alert_critical:
                data["push"] = {"interruption-level": "critical",
                                "sound": {"name": "default", "critical": 1, "volume": 1.0}}
                data["channel"] = "alarm_stream"
        return data

    def notify(self, title: str, message: str, kind: str = "summary", tag: str = "market_monitor_update") -> list[str]:
        """Stuurt een popup naar alle telefoons. Geeft de gebruikte services terug."""
        if not self.enabled:
            return []
        targets = self.notify_targets()
        data = self.build_notify_data(kind, tag)
        sent = []
        for svc in targets:
            if self.call_service("notify", svc, {"title": title, "message": message, "data": data}):
                sent.append(svc)
        return sent

    def send_test(self) -> dict:
        if not self.token:
            return {"ok": False, "message": "Testmelding werkt alleen als de add-on in Home Assistant draait."}
        if not self.cfg.publish_to_home_assistant:
            return {"ok": False, "message": "Zet 'publish_to_home_assistant' aan bij de opties."}
        self._targets = None
        sent = self.notify("Market Monitor: testmelding",
                           "Werkt! Zo zien het uurbericht en noodmeldingen eruit. Tik om het dashboard te openen.",
                           kind="summary", tag="market_monitor_test")
        if sent:
            return {"ok": True, "targets": sent, "message": "Verstuurd naar: " + ", ".join(sent)}
        return {"ok": False, "targets": [],
                "message": "Geen telefoon gevonden of verzenden mislukt. Installeer de Home Assistant-app, log in en "
                           "sta meldingen toe, of vul 'notify_service' in (bv. mobile_app_jouw_telefoon)."}

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

    def publish_recommendation(self, market: str) -> None:
        if not self.enabled:
            return
        rec = self.db.get_state(f"recommendation_{market}")
        if not rec:
            return
        state = f"{rec['label']} {rec['ticker']}" if rec["direction"] != "neutral" else f"AFWACHTEN ({rec['ticker']})"
        self.set_state(
            f"sensor.market_monitor_{market}_recommendation", state,
            {"friendly_name": f"Market Monitor {MARKET_LABELS[market].lower()}: aanbeveling",
             "icon": "mdi:chart-timeline-variant-shimmer",
             "status": (rec.get("status") or {}).get("text"),
             **{k: rec.get(k) for k in ("ticker", "name", "direction", "action", "conviction", "price", "live_price",
                                        "entry_low", "entry_high", "stop", "target1", "target2", "rr", "prob",
                                        "signals", "since", "updated_at", "change_reason", "target1_hit")}},
        )

    def notify_recommendation(self, market: str, res: dict) -> None:
        """Popup als de aanbeveling wijzigt. Stop/doel altijd direct, overige wijzigingen max. 1x per 30 min."""
        if not (self.enabled and self.cfg.notify_recommendation_changes):
            return
        event, st = res.get("event"), res["state"]
        key = f"reco_notify_{market}"
        last = parse_iso(self.db.get_state(key))
        if event not in ("stop", "target1", "target2") and last and utcnow() - last < timedelta(minutes=30):
            return
        label = MARKET_LABELS[market].lower()
        if event == "target1":
            title = f"Doel 1 gehaald: {st['label']} {st['ticker']}"
            message = (f"Koers {fmt_price(st.get('live_price'))}. Overweeg (een deel van de) winst te nemen; "
                       f"de stop staat nu op de instapprijs {fmt_price(st['stop'])}.")
        else:
            now_txt = (f"{st['label']} {st['ticker']}" if st["direction"] != "neutral"
                       else f"afwachten ({st['ticker']})")
            title = f"Aanbeveling {label}: {now_txt}"
            line = recommendation_line(st).removeprefix("Aanbeveling: ")
            message = f"{res.get('reason', '')}.\nNu: {line}" if res.get("reason") else f"Nu: {line}"
        self.notify(title, message, kind="summary", tag=f"market_monitor_reco_{market}")
        self.db.set_state(key, iso(utcnow()))

    def publish_summaries(self, markets: list[str]) -> None:
        """Werkt de sensoren bij en stuurt één popup met beide markten (uurbericht)."""
        lines = []
        for market in markets:
            s = latest_summary(self.db, market)
            if not s:
                continue
            c = s["content"]
            text = "\n".join(f"- {p}" for p in c.get("key_points", []))
            lines.append(f"{MARKET_LABELS[market]}: {c.get('headline', '')}")
            rec_line = recommendation_line(self.db.get_state(f"recommendation_{market}"))
            if rec_line:
                lines.append(rec_line)
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
                    "aanbeveling": c.get("recommendation_comment"),
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
        if not (self.enabled and lines and self.cfg.notify_summaries):
            return
        if self.cfg.quiet_hours and in_quiet_hours(self.cfg.quiet_hours, local_hour()):
            log.info("Stille uren: geen pushbericht voor het uurbericht")
            return
        self.notify(f"Markt-update {_now_hhmm()}", "\n".join(lines), kind="summary", tag="market_monitor_update")

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
        """Noodmelding: direct als popup op de telefoon, plus HA-melding, event en sensor."""
        label = MARKET_LABELS.get(alert["market"], alert["market"])
        title = f"Noodmelding {label.lower()}: {alert['title']}"
        message = alert.get("message") or ""
        if alert.get("ai_note"):
            message += f"\n\nAI: {alert['ai_note']}"
        message += "\n\nGeen beleggingsadvies; een signaal is geen garantie."
        if not self.enabled:
            return
        self.notify(title, message, kind="alert", tag=f"market_monitor_alert_{alert['id']}")
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


def _now_hhmm() -> str:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(os.environ.get("TZ", "Europe/Amsterdam"))).strftime("%H:%M")
    except Exception:  # noqa: BLE001
        return datetime.now().strftime("%H:%M")
