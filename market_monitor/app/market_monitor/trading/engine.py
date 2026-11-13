"""Handelsmotor: draait elke minuut (zie app.py) voor elke actieve run (paper-tests en de live-run).

Per run en per minuut:
  1. posities waarderen tegen de actuele koers (in euro, met de ECB-koers)
  2. stops, doelen, trailing stop en maximale looptijd controleren -> zo nodig (deels) sluiten
  3. risicobewaking: daglimiet (geen nieuwe trades meer vandaag) en noodrem (run stopt)
  4. elke 'decision_minutes' (of direct bij groot nieuws): signalen berekenen -> voorstellen van de regels
     -> Claude beslist (of de regels als er geen AI is) -> positiegrootte op basis van risico -> uitvoeren
  5. vermogen vastleggen voor de grafiek

Alle bedragen in de administratie zijn in euro; koersniveaus (instap, stop, doelen) in dollar, zoals in het
dashboard. Een paper-test stopt na de gekozen duur en rekent dan alle posities af.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from datetime import datetime, timedelta

from ..config import MARKET_LABELS
from ..db import iso, parse_iso, utcnow
from ..net import make_session
from ..scoring import fmt_num, fmt_price
from . import settings as tsettings
from .ai import AiTrader
from .brokers import BitvavoBroker, BrokerError, Order, PaperBroker, Trading212Broker
from .fx import FxRate
from .signals import SignalEngine, levels_for, market_open

log = logging.getLogger(__name__)

CONFIRM_TEXT = "IK BEGRIJP HET RISICO"
MAX_PAPER_RUNS = 5
MIN_TRIGGER_GAP = timedelta(minutes=3)
REJECT_REPEAT = timedelta(hours=2)
STOP_MOVE_MIN = 0.005          # Bitvavo-stop pas verplaatsen bij >0,5% verschil
EPS = 1e-9


def local_day(now: datetime) -> str:
    try:
        from zoneinfo import ZoneInfo

        return now.astimezone(ZoneInfo(os.environ.get("TZ", "Europe/Amsterdam"))).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return now.strftime("%Y-%m-%d")


def fmt_eur(v: float | None) -> str:
    if v is None:
        return "?"
    return ("-" if v < 0 else "") + "€" + f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_px(v: float | None) -> str:
    """Koers zonder dollarteken, Nederlandse notatie (bv. 187,29 of 0,00001077)."""
    return fmt_price(v).removeprefix("$")


def fmt_qty(q: float) -> str:
    if q >= 1000:
        return fmt_num(q, 0)
    if q >= 1:
        return fmt_num(q, 4).rstrip("0").rstrip(",")
    return fmt_num(q, 8).rstrip("0").rstrip(",")


class TradingEngine:
    def __init__(self, cfg, db, recommender, summarizer=None, hass=None, session=None, broker_factory=None,
                 clock=None, fx=None):
        self.cfg = cfg
        self.db = db
        self.hass = hass
        self.signals = SignalEngine(db, recommender)
        self.fx = fx or FxRate(db, session or make_session())
        self.ai = AiTrader(cfg, db, summarizer)
        self.broker_factory = broker_factory or self._make_broker
        self.clock = clock or utcnow
        self._lock = threading.RLock()
        self._brokers: dict[tuple[int, str], object] = {}
        self._rejections: dict[tuple, datetime] = {}
        self._last_publish: datetime | None = None

    # ================================================================== instellingen en runs
    def settings(self) -> dict:
        return tsettings.load(self.db)

    def save_settings(self, values: dict) -> tuple[dict, dict]:
        return tsettings.save(self.db, values)

    def live_checks(self, settings: dict | None = None) -> list[dict]:
        s = settings or self.settings()
        checks = [
            {"ok": bool(self.cfg.allow_live_trading),
             "text": "Add-on optie 'allow_live_trading' staat aan" if self.cfg.allow_live_trading
             else "Zet de add-on optie 'allow_live_trading' aan (hoofdschakelaar voor echt geld)"},
        ]
        if s["budget_crypto_eur"] > 0:
            ok = bool(self.cfg.bitvavo_api_key and self.cfg.bitvavo_api_secret)
            checks.append({"ok": ok, "text": "Bitvavo API-sleutel en -geheim ingevuld" if ok
                           else "Vul 'bitvavo_api_key' en 'bitvavo_api_secret' in bij de add-on opties"})
        if s["budget_stocks_eur"] > 0:
            ok = bool(self.cfg.trading212_api_key)
            checks.append({"ok": ok, "text": f"Trading 212 API-sleutel ingevuld (omgeving: {s['trading212_env']})"
                           if ok else "Vul 'trading212_api_key' (en 'trading212_api_secret') in bij de add-on opties"})
        if s["budget_crypto_eur"] <= 0 and s["budget_stocks_eur"] <= 0:
            checks.append({"ok": False, "text": "Zet een budget voor aandelen en/of crypto"})
        running = self.db.query_one("SELECT id FROM trading_runs WHERE mode='live' AND status='running'")
        checks.append({"ok": running is None, "text": "Er draait nog geen live-run" if running is None
                       else "Er draait al een live-run: stop die eerst"})
        return checks

    def start_run(self, mode: str, name: str = "", hours: float | None = None, confirm: str = "",
                  settings: dict | None = None) -> dict:
        s = dict(settings or self.settings())
        now = self.clock()
        if mode not in ("paper", "live"):
            raise ValueError("onbekende modus")
        if mode == "paper":
            n = self.db.query_one("SELECT COUNT(*) n FROM trading_runs WHERE mode='paper' AND status='running'")["n"]
            if n >= MAX_PAPER_RUNS:
                raise ValueError(f"Er draaien al {MAX_PAPER_RUNS} paper-tests: stop er eerst een")
            if hours is not None and not (0.25 <= hours <= 24 * 60):
                raise ValueError("Kies een duur tussen 15 minuten en 60 dagen")
        else:
            failed = [c["text"] for c in self.live_checks(s) if not c["ok"]]
            if failed:
                raise ValueError("Live starten kan nog niet: " + "; ".join(failed))
            if not s["live_dry_run"] and confirm.strip().upper() != CONFIRM_TEXT:
                raise ValueError(f"Typ ter bevestiging: {CONFIRM_TEXT}")
            s["max_exposure_pct"] = min(s["max_exposure_pct"], 100.0)
        capital = {"stocks": float(s["budget_stocks_eur"]) if self.cfg.enable_stocks else 0.0,
                   "crypto": float(s["budget_crypto_eur"]) if self.cfg.enable_crypto else 0.0}
        total = sum(capital.values())
        if total <= 0:
            raise ValueError("Zet een budget voor aandelen en/of crypto")
        state = {"cash": dict(capital), "peak": total, "day": local_day(now), "day_start": total,
                 "trades_today": {"stocks": 0, "crypto": 0}, "last_decision": {}, "last_ai": {}, "ai_day": "",
                 "ai_calls_today": 0, "cooldown": {}, "market_view": {}, "last_tick": iso(now),
                 "trigger_seen": {"stocks": iso(now), "crypto": iso(now)}}
        if not name:
            name = ("Paper-test" if mode == "paper" else ("Live (proefdraaien)" if s["live_dry_run"] else "Live")) \
                + " " + local_day(now)[5:] + " " + now.astimezone().strftime("%H:%M")
        ends = iso(now + timedelta(hours=hours)) if (mode == "paper" and hours) else None
        self.db.execute(
            "INSERT INTO trading_runs(name, mode, status, created_at, ends_at, settings, capital, state)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (name[:80], mode, "running", iso(now), ends, json.dumps(s), json.dumps(capital), json.dumps(state)),
        )
        run_id = self.db.query_one("SELECT MAX(id) id FROM trading_runs")["id"]
        markets = ", ".join(f"{MARKET_LABELS[m].lower()} {fmt_eur(v)}" for m, v in capital.items() if v > 0)
        extra = ""
        if mode == "live":
            extra = " Proefdraaien: er worden geen echte orders verstuurd." if s["live_dry_run"] else \
                " ECHT GELD. Live kan alleen long (geen short via de API's)."
        self.log(run_id, None, None, "info", "info", "systeem",
                 f"Run gestart ({'paper' if mode == 'paper' else 'live'}): {markets}"
                 + (f", duur {hours:g} uur" if ends else "") + "." + extra)
        if mode == "live":
            self.notify_run({"id": run_id, "mode": mode, "settings": s}, f"Live handelen gestart: {name}",
                            f"Budget: {markets}.{extra}", force=True)
        return self.get_run(run_id)

    def get_run(self, run_id: int) -> dict | None:
        r = self.db.query_one("SELECT * FROM trading_runs WHERE id=?", (run_id,))
        if not r:
            return None
        for k in ("settings", "capital", "state", "stats"):
            r[k] = json.loads(r[k]) if r.get(k) else ({} if k != "stats" else None)
        return r

    def running_runs(self) -> list[dict]:
        ids = [r["id"] for r in self.db.query("SELECT id FROM trading_runs WHERE status='running' ORDER BY id")]
        return [self.get_run(i) for i in ids]

    def save_state(self, run: dict) -> None:
        self.db.execute("UPDATE trading_runs SET state=? WHERE id=?", (json.dumps(run["state"]), run["id"]))

    def stop_run(self, run_id: int, close_positions: bool = True, reason: str = "Handmatig gestopt",
                 status: str = "stopped") -> dict | None:
        with self._lock:
            run = self.get_run(run_id)
            if not run or run["status"] != "running":
                return run
            now = self.clock()
            if close_positions:
                self.close_all(run, now, reason, force=True)
            run["status"] = status
            self.db.execute("UPDATE trading_runs SET status=?, ended_at=?, halt_reason=?, state=? WHERE id=?",
                            (status, iso(now), reason, json.dumps(run["state"]), run_id))
            self.snapshot(run, now, force=True)
            st = self.stats(run_id)
            self.db.execute("UPDATE trading_runs SET stats=? WHERE id=?", (json.dumps(st), run_id))
            self.log(run_id, None, None, "halt" if status == "halted" else "info", "info", "systeem",
                     f"Run beëindigd: {reason}. Resultaat {st['return_pct']:+.2f}% ({fmt_eur(st['pnl_eur'])}).")
            for key in [k for k in self._brokers if k[0] == run_id]:
                self._brokers.pop(key, None)
            return self.get_run(run_id)

    # ================================================================== brokers en koersen
    def _make_broker(self, run: dict, market: str):
        s = run["settings"]
        if run["mode"] == "paper":
            return PaperBroker(s)
        if market == "crypto":
            return BitvavoBroker(self.cfg.bitvavo_api_key, self.cfg.bitvavo_api_secret, s, s["live_dry_run"])
        return Trading212Broker(self.cfg.trading212_api_key, self.cfg.trading212_api_secret, s, s["live_dry_run"],
                                db=self.db)

    def broker(self, run: dict, market: str):
        key = (run["id"], market)
        if key not in self._brokers:
            self._brokers[key] = self.broker_factory(run, market)
        return self._brokers[key]

    def quote(self, market: str, ticker: str, now: datetime) -> tuple[float | None, float | None]:
        m = self.db.query_one("SELECT last_price, fetched_at FROM price_meta WHERE market=? AND ticker=?",
                              (market, ticker))
        if not m or not m.get("last_price"):
            return None, None
        ts = parse_iso(m.get("fetched_at"))
        age = (now - ts).total_seconds() / 60 if ts else None
        return m["last_price"], age

    def priority_tickers(self, market: str) -> list[str]:
        """Aandelen met een open positie: hun koers moet tijdens beurstijd elk kwartier vers zijn."""
        rows = self.db.query(
            "SELECT DISTINCT p.ticker FROM trading_positions p JOIN trading_runs r ON r.id=p.run_id"
            " WHERE p.status='open' AND r.status='running' AND p.market=?", (market,))
        return [r["ticker"] for r in rows]

    # ================================================================== administratie
    def positions(self, run_id: int, market: str | None = None, status: str = "open") -> list[dict]:
        sql = "SELECT * FROM trading_positions WHERE run_id=? AND status=?"
        args: list = [run_id, status]
        if market:
            sql += " AND market=?"
            args.append(market)
        return self.db.query(sql + " ORDER BY id", args)

    def portfolio(self, run: dict, fx: float, now: datetime) -> dict:
        out = {"markets": {}, "equity": 0.0, "exposure": 0.0, "cash": 0.0}
        for m in ("stocks", "crypto"):
            cap = run["capital"].get(m, 0)
            cash = run["state"]["cash"].get(m, 0.0)
            pos = self.positions(run["id"], m)
            long_v = short_v = 0.0
            rows = []
            for p in pos:
                price = p.get("last_price") or p["entry_price"]
                val = p["qty"] * price / fx
                if p["side"] == "long":
                    long_v += val
                    upnl = (price / fx - p["entry_price_eur"]) * p["qty"]
                else:
                    short_v += val
                    upnl = (p["entry_price_eur"] - price / fx) * p["qty"]
                rows.append({**p, "price": price, "value_eur": val, "upnl_eur": upnl,
                             "upnl_pct": (upnl / (p["entry_price_eur"] * p["qty"]) * 100) if p["qty"] else 0.0})
            equity = cash + long_v - short_v
            out["markets"][m] = {"capital": cap, "cash": cash, "long_value": long_v, "short_value": short_v,
                                 "equity": equity, "exposure": long_v + short_v, "positions": rows}
            if cap > 0 or pos:
                out["equity"] += equity
                out["exposure"] += long_v + short_v
                out["cash"] += cash
        return out

    def log(self, run_id: int, market: str | None, ticker: str | None, action: str, outcome: str, actor: str,
            reason: str, conviction: float | None = None, size_eur: float | None = None,
            details: dict | None = None) -> None:
        self.db.execute(
            "INSERT INTO trading_decisions(run_id, ts, market, ticker, action, outcome, actor, conviction, size_eur,"
            " reason, details) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, iso(self.clock()), market, ticker, action, outcome, actor, conviction,
             round(size_eur, 2) if size_eur is not None else None, reason[:1000],
             json.dumps(details, ensure_ascii=False, default=str) if details else None),
        )

    def _reject(self, run: dict, market: str, sig: dict, code: str, reason: str, actor: str = "regels",
                now: datetime | None = None) -> None:
        """Afwijzing loggen, maar dezelfde reden voor dezelfde ticker hooguit eens per 2 uur."""
        key = (run["id"], market, sig["ticker"], code)
        now = now or self.clock()
        last = self._rejections.get(key)
        if last and now - last < REJECT_REPEAT:
            return
        self._rejections[key] = now
        self.log(run["id"], market, sig["ticker"], f"open_{sig['side']}", "afgewezen", actor, reason,
                 conviction=sig["conviction"])

    def notify_run(self, run: dict, title: str, message: str, kind: str = "summary", force: bool = False) -> None:
        if not self.hass:
            return
        pref = run["settings"].get("notify_trades", "live")
        if not force and (pref == "uit" or (pref == "live" and run["mode"] != "live")):
            return
        try:
            self.hass.notify(title, message, kind=kind, tag=f"market_monitor_trade_{run['id']}")
        except Exception as exc:  # noqa: BLE001
            log.warning("Melding versturen mislukt: %s", exc)

    # ================================================================== uitvoeren
    def _execute(self, run: dict, market: str, ticker: str, side: str, intent: str, ref_price: float, fx: float,
                 qty: float | None = None, eur: float | None = None) -> dict | None:
        broker = self.broker(run, market)
        order = Order(market=market, ticker=ticker, side=side, ref_price=ref_price, fx=fx, qty=qty, eur=eur,
                      intent=intent)
        try:
            return broker.execute(order)
        except BrokerError as exc:
            self.log(run["id"], market, ticker, intent, "fout", "systeem", f"Order mislukt: {exc}")
            if exc.uncertain:
                self.halt(run, self.clock(), f"Onzeker of een order is uitgevoerd ({exc}). Controleer je account "
                                             f"bij {getattr(broker, 'name', 'de broker')}.", close=False)
            return None

    def _record_fill(self, run: dict, pos_id: int | None, market: str, ticker: str, action: str, fill: dict,
                     pnl: float | None, broker_name: str) -> None:
        self.db.execute(
            "INSERT INTO trading_fills(run_id, position_id, ts, market, ticker, action, qty, price, price_eur,"
            " value_eur, fee_eur, pnl_eur, broker, order_id, dry_run, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run["id"], pos_id, iso(self.clock()), market, ticker, action, fill["qty"], fill["price"],
             fill["price_eur"], fill["value_eur"], fill["fee_eur"], pnl, broker_name, fill.get("order_id"),
             1 if fill.get("dry_run") else 0, fill.get("note")),
        )

    def open_position(self, run: dict, market: str, sig: dict, side: str, lv: dict, value_eur: float, fx: float,
                      reason: str, actor: str, conviction: float, now: datetime) -> dict | None:
        intent = f"open_{side}"
        fill = self._execute(run, market, sig["ticker"], "buy" if side == "long" else "sell", intent, sig["price"],
                             fx, eur=value_eur)
        if not fill or fill["qty"] <= 0:
            return None
        cash = run["state"]["cash"]
        if side == "long":
            cash[market] -= fill["value_eur"] + fill["fee_eur"]
        else:
            cash[market] += fill["value_eur"] - fill["fee_eur"]
        # Niveaus meeschuiven met de werkelijke uitvoeringsprijs
        shift = fill["price"] - sig["price"]
        stop, t1, t2 = lv["stop"] + shift, lv["target1"] + shift, lv["target2"] + shift
        details = {"components": sig["components"], "rr": lv["rr"], "plan_based": lv.get("plan_based"),
                   "signals": sig.get("signals", [])[:4]}
        self.db.execute(
            "INSERT INTO trading_positions(run_id, market, ticker, side, status, qty, qty_initial, entry_price,"
            " entry_price_eur, opened_at, stop, target1, target2, atr, high_water, low_water, conviction, source,"
            " reason_open, pnl_eur, fees_eur, last_price, details) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run["id"], market, sig["ticker"], side, "open", fill["qty"], fill["qty"], fill["price"],
             fill["price_eur"], iso(now), stop, t1, t2, lv["atr"], fill["price"], fill["price"], conviction, actor,
             reason[:1000], -fill["fee_eur"], fill["fee_eur"], fill["price"], json.dumps(details, default=str)),
        )
        pos_id = self.db.query_one("SELECT MAX(id) id FROM trading_positions WHERE run_id=?", (run["id"],))["id"]
        broker = self.broker(run, market)
        self._record_fill(run, pos_id, market, sig["ticker"], "buy" if side == "long" else "short", fill, None,
                          getattr(broker, "name", "paper"))
        st = run["state"]
        st["trades_today"][market] = st["trades_today"].get(market, 0) + 1
        label = "LONG" if side == "long" else "SHORT"
        dry = " (proefdraaien)" if fill.get("dry_run") else ""
        self.log(run["id"], market, sig["ticker"], intent, "uitgevoerd", actor,
                 f"{label} {sig['ticker']} geopend{dry}: {fmt_qty(fill['qty'])} stuks à ${fmt_px(fill['price'])} "
                 f"({fmt_eur(fill['value_eur'])}). Stop ${fmt_px(stop)}, doel ${fmt_px(t1)} / ${fmt_px(t2)}, "
                 f"R/R {fmt_num(lv['rr'], 1)}. {reason}", conviction=conviction, size_eur=fill["value_eur"])
        self.notify_run(run, f"{label} {sig['ticker']} geopend{dry}",
                        f"{fmt_eur(fill['value_eur'])} à ${fmt_px(fill['price'])}. Stop ${fmt_px(stop)}, "
                        f"doel ${fmt_px(t1)}. {reason[:180]}")
        self.save_state(run)
        if run["mode"] == "live" and market == "crypto" and side == "long" and run["settings"]["bitvavo_exchange_stop"]:
            self._place_exchange_stop(run, pos_id, sig["ticker"], fill["qty"], stop, fx)
        return self.db.query_one("SELECT * FROM trading_positions WHERE id=?", (pos_id,))

    def close_position(self, run: dict, pos: dict, qty: float | None, price: float, fx: float, reason: str,
                       actor: str, now: datetime, fill: dict | None = None) -> float | None:
        """Sluit (een deel van) een positie. 'fill' meegeven als de order al is uitgevoerd (bv. Bitvavo-stop)."""
        market = pos["market"]
        q = min(pos["qty"], qty if qty is not None else pos["qty"])
        if q <= EPS:
            return None
        live_stop = run["mode"] == "live" and pos.get("stop_order_id")
        if fill is None and live_stop:
            # Eerst de stop-order op de beurs annuleren, anders kan er dubbel verkocht worden
            broker = self.broker(run, market)
            try:
                status = broker.order_status(pos["ticker"], pos["stop_order_id"])
            except BrokerError:
                status = {}
            if status.get("status") == "filled":
                return self._exchange_stop_filled(run, pos, status, fx, now)
            broker.cancel_order(pos["ticker"], pos["stop_order_id"])
            self.db.execute("UPDATE trading_positions SET stop_order_id=NULL WHERE id=?", (pos["id"],))
            pos["stop_order_id"] = None
        side = "sell" if pos["side"] == "long" else "buy"
        if fill is None:
            fill = self._execute(run, market, pos["ticker"], side, f"close_{pos['side']}", price, fx, qty=q)
            if not fill:
                if live_stop and run["settings"]["bitvavo_exchange_stop"] and run["status"] == "running":
                    # Verkoop mislukt: de beschermende stop-order op de beurs terugzetten
                    self._place_exchange_stop(run, pos["id"], pos["ticker"], pos["qty"], pos["stop"], fx)
                return None
        q = min(q, fill["qty"]) if fill["qty"] > 0 else q
        cash = run["state"]["cash"]
        if pos["side"] == "long":
            pnl = (fill["price_eur"] - pos["entry_price_eur"]) * q - fill["fee_eur"]
            cash[market] += fill["price_eur"] * q - fill["fee_eur"]
        else:
            pnl = (pos["entry_price_eur"] - fill["price_eur"]) * q - fill["fee_eur"]
            cash[market] -= fill["price_eur"] * q + fill["fee_eur"]
        remaining = pos["qty"] - q
        closed = remaining <= max(EPS, pos["qty_initial"] * 1e-6)
        total_pnl = (pos["pnl_eur"] or 0) + pnl
        self.db.execute(
            "UPDATE trading_positions SET qty=?, pnl_eur=?, fees_eur=fees_eur+?, status=?, closed_at=?, exit_price=?,"
            " reason_close=?, last_price=? WHERE id=?",
            (0.0 if closed else remaining, total_pnl, fill["fee_eur"], "closed" if closed else "open",
             iso(now) if closed else None, fill["price"] if closed else None, reason if closed else pos.get("reason_close"),
             fill["price"], pos["id"]),
        )
        broker = self.broker(run, market)
        self._record_fill(run, pos["id"], market, pos["ticker"], "sell" if pos["side"] == "long" else "cover", fill,
                          pnl, getattr(broker, "name", "paper"))
        what = "gesloten" if closed else f"deels gesloten ({q / pos['qty'] * 100:.0f}%)"
        dry = " (proefdraaien)" if fill.get("dry_run") else ""
        self.log(run["id"], market, pos["ticker"], "close" if closed else "reduce", "uitgevoerd", actor,
                 f"{pos['side'].upper()} {pos['ticker']} {what}{dry} à ${fmt_px(fill['price'])}: "
                 f"{fmt_eur(pnl)}. {reason}", size_eur=fill["value_eur"])
        self.notify_run(run, f"{pos['ticker']} {what}{dry}: {fmt_eur(pnl)}",
                        f"{pos['side'].upper()} à ${fmt_px(fill['price'])}. {reason[:180]}")
        if closed and total_pnl < 0 and run["settings"]["cooldown_after_stop_hours"] > 0 and "stop" in reason.lower():
            until = now + timedelta(hours=run["settings"]["cooldown_after_stop_hours"])
            run["state"]["cooldown"][f"{market}:{pos['ticker']}:{pos['side']}"] = iso(until)
        self.save_state(run)
        if not closed and live_stop and run["settings"]["bitvavo_exchange_stop"]:
            self._place_exchange_stop(run, pos["id"], pos["ticker"], remaining, pos["stop"], fx)
        return pnl

    def close_all(self, run: dict, now: datetime, reason: str, force: bool = False) -> int:
        """Sluit alle posities. Paper: ook als de beurs dicht is (tegen de laatste koers). Live: aandelen alleen
        tijdens beurstijd - anders blijft de positie staan en staat dat in het logboek."""
        fx = self.fx.eurusd()
        n = 0
        for pos in self.positions(run["id"]):
            price, _age = self.quote(pos["market"], pos["ticker"], now)
            price = price or pos.get("last_price") or pos["entry_price"]
            note = reason
            if not market_open(pos["market"], run["settings"], now):
                if run["mode"] == "live":
                    self.log(run["id"], pos["market"], pos["ticker"], "close", "afgewezen", "systeem",
                             f"{pos['ticker']} niet gesloten: de beurs is dicht. Sluit de positie zelf in de app van "
                             "de broker, of stop opnieuw tijdens beurstijd.")
                    continue
                note += " (beurs dicht: afgerekend tegen de laatste koers)"
            if self.close_position(run, pos, None, price, fx, note, "systeem", now) is not None:
                n += 1
        return n

    # ================================================================== Bitvavo-stop op de beurs
    def _place_exchange_stop(self, run: dict, pos_id: int, ticker: str, qty: float, stop: float, fx: float) -> None:
        broker = self.broker(run, "crypto")
        if not hasattr(broker, "place_stop"):
            return
        try:
            oid = broker.place_stop(ticker, qty, stop, fx)
        except BrokerError as exc:
            self.log(run["id"], "crypto", ticker, "stop", "fout", "systeem",
                     f"Stop-loss op Bitvavo plaatsen mislukt ({exc}); de Pi bewaakt de stop wel zelf.")
            return
        if oid:
            row = self.db.query_one("SELECT details FROM trading_positions WHERE id=?", (pos_id,)) or {}
            details = json.loads(row.get("details") or "{}")
            details["exchange_stop"] = stop
            self.db.execute("UPDATE trading_positions SET stop_order_id=?, details=? WHERE id=?",
                            (oid, json.dumps(details, default=str), pos_id))

    def _move_exchange_stop(self, run: dict, pos: dict, new_stop: float, fx: float) -> None:
        if not (run["mode"] == "live" and pos["market"] == "crypto" and pos.get("stop_order_id")):
            return
        details = json.loads(pos.get("details") or "{}")
        old = details.get("exchange_stop") or pos["stop"]
        if old and abs(new_stop / old - 1) < STOP_MOVE_MIN:
            return
        broker = self.broker(run, "crypto")
        broker.cancel_order(pos["ticker"], pos["stop_order_id"])
        self.db.execute("UPDATE trading_positions SET stop_order_id=NULL WHERE id=?", (pos["id"],))
        self._place_exchange_stop(run, pos["id"], pos["ticker"], pos["qty"], new_stop, fx)

    def _exchange_stop_filled(self, run: dict, pos: dict, status: dict, fx: float, now: datetime) -> float | None:
        qty = float(status.get("filledAmount") or 0) or pos["qty"]
        quote = float(status.get("filledAmountQuote") or 0)
        price_eur = quote / qty if quote and qty else (pos.get("last_price") or pos["entry_price"]) / fx
        fee = float(status.get("feePaid") or 0) if (status.get("feeCurrency") or "EUR").upper() == "EUR" else 0.0
        fill = {"qty": qty, "price": price_eur * fx, "price_eur": price_eur, "value_eur": qty * price_eur,
                "fee_eur": fee, "order_id": pos["stop_order_id"], "dry_run": False, "note": "Bitvavo stop-loss-order"}
        self.db.execute("UPDATE trading_positions SET stop_order_id=NULL WHERE id=?", (pos["id"],))
        pos["stop_order_id"] = None
        return self.close_position(run, pos, qty, fill["price"], fx, "Stop-loss op Bitvavo uitgevoerd", "systeem",
                                   now, fill=fill)

    def _sync_exchange_stops(self, run: dict, fx: float, now: datetime) -> None:
        if run["mode"] != "live" or run["settings"]["live_dry_run"]:
            return
        last = parse_iso(run["state"].get("stop_sync"))
        if last and now - last < timedelta(minutes=2):
            return
        run["state"]["stop_sync"] = iso(now)
        broker = self.broker(run, "crypto")
        for pos in self.positions(run["id"], "crypto"):
            if not pos.get("stop_order_id"):
                continue
            try:
                status = broker.order_status(pos["ticker"], pos["stop_order_id"])
            except BrokerError as exc:
                log.warning("Stop-order %s controleren mislukt: %s", pos["ticker"], exc)
                continue
            if status.get("status") == "filled":
                self._exchange_stop_filled(run, pos, status, fx, now)
            elif status.get("status") in ("cancelled", "expired", "rejected"):
                self.db.execute("UPDATE trading_positions SET stop_order_id=NULL WHERE id=?", (pos["id"],))
                self._place_exchange_stop(run, pos["id"], pos["ticker"], pos["qty"], pos["stop"], fx)

    # ================================================================== de minuut-tik
    def tick(self, now: datetime | None = None) -> int:
        with self._lock:
            now = now or self.clock()
            n = 0
            for run in self.running_runs():
                try:
                    self.process(run, now)
                    n += 1
                except Exception as exc:  # noqa: BLE001 - één run mag de andere niet blokkeren
                    log.exception("Handelsrun %s mislukt", run["id"])
                    self.log(run["id"], None, None, "info", "fout", "systeem", f"Fout in de handelsmotor: {exc}")
            self.publish(now)
            return n

    def process(self, run: dict, now: datetime) -> None:
        s, st = run["settings"], run["state"]
        fx = self.fx.eurusd()
        # ------------------------------------------------ nieuwe dag
        day = local_day(now)
        if st.get("day") != day:
            port = self.portfolio(run, fx, now)
            st.update(day=day, day_start=port["equity"], trades_today={"stocks": 0, "crypto": 0}, paused=None)
        # ------------------------------------------------ koersen bijwerken + leenkosten short
        last_tick = parse_iso(st.get("last_tick")) or now
        dt_days = max(0.0, (now - last_tick).total_seconds() / 86400)
        st["last_tick"] = iso(now)
        for pos in self.positions(run["id"]):
            price, age = self.quote(pos["market"], pos["ticker"], now)
            if price is None:
                continue
            hw, lw = max(pos["high_water"] or price, price), min(pos["low_water"] or price, price)
            self.db.execute("UPDATE trading_positions SET last_price=?, high_water=?, low_water=? WHERE id=?",
                            (price, hw, lw, pos["id"]))
            if pos["side"] == "short" and run["mode"] == "paper" and dt_days > 0:
                fee = pos["qty"] * price / fx * s["short_fee_pct_day"] / 100 * dt_days
                st["cash"][pos["market"]] -= fee
                self.db.execute("UPDATE trading_positions SET pnl_eur=pnl_eur-?, fees_eur=fees_eur+? WHERE id=?",
                                (fee, fee, pos["id"]))
        # ------------------------------------------------ einde paper-test
        if run["mode"] == "paper" and run.get("ends_at") and now >= parse_iso(run["ends_at"]):
            self.save_state(run)
            self.stop_run(run["id"], True, "Testduur voorbij", status="finished")
            run = self.get_run(run["id"])
            st_ = run.get("stats") or {}
            self.notify_run(run, f"Paper-test klaar: {st_.get('return_pct', 0):+.2f}%",
                            f"{run['name']}: {fmt_eur(st_.get('pnl_eur'))}, {st_.get('trades', 0)} trades, "
                            f"win-rate {st_.get('win_rate') or 0:.0f}%. Bekijk het tabblad Trading.", force=True)
            return
        # ------------------------------------------------ stops, doelen, trailing
        self._sync_exchange_stops(run, fx, now)
        for pos in self.positions(run["id"]):
            self._manage(run, pos, fx, now)
        # ------------------------------------------------ risico
        port = self.portfolio(run, fx, now)
        if port["equity"] > st.get("peak", 0):
            st["peak"] = port["equity"]
        dd = 1 - port["equity"] / st["peak"] if st.get("peak") else 0
        if dd * 100 >= s["max_drawdown_pct"]:
            self.halt(run, now, f"Noodrem: vermogen {dd * 100:.1f}% onder de hoogste stand "
                                f"(grens {s['max_drawdown_pct']:g}%)", close=s["close_on_halt"])
            return
        day_loss = 1 - port["equity"] / st["day_start"] if st.get("day_start") else 0
        if day_loss * 100 >= s["max_daily_loss_pct"] and not st.get("paused"):
            st["paused"] = {"day": day, "reason": f"Daglimiet: {day_loss * 100:.1f}% verlies vandaag"}
            self.log(run["id"], None, None, "halt", "info", "risico",
                     f"Daglimiet bereikt ({day_loss * 100:.1f}% verlies vandaag, grens {s['max_daily_loss_pct']:g}%):"
                     " vandaag geen nieuwe posities meer." + (" Posities worden gesloten." if s["close_on_halt"] else ""))
            self.notify_run(run, "Daglimiet bereikt", f"{run['name']}: {day_loss * 100:.1f}% verlies vandaag. "
                            "Geen nieuwe trades meer vandaag.", kind="alert", force=True)
            if s["close_on_halt"]:
                self.close_all(run, now, "Daglimiet bereikt")
        # ------------------------------------------------ beslissen
        for market in ("stocks", "crypto"):
            if run["capital"].get(market, 0) <= 0 and not self.positions(run["id"], market):
                continue
            if not market_open(market, s, now):
                continue
            due, why = self._decision_due(run, market, now)
            if due:
                self.decide(run, market, now, fx, trigger=why)
        self.snapshot(run, now)
        self.save_state(run)

    def halt(self, run: dict, now: datetime, reason: str, close: bool = True) -> None:
        self.save_state(run)
        self.stop_run(run["id"], close, reason, status="halted")
        self.notify_run(run, "Handelen gestopt (noodrem)", f"{run['name']}: {reason}", kind="alert", force=True)

    # ------------------------------------------------------------------ positie beheren
    def _manage(self, run: dict, pos: dict, fx: float, now: datetime) -> None:
        s = run["settings"]
        price, age = self.quote(pos["market"], pos["ticker"], now)
        if price is None or age is None or age > s["max_price_age_minutes"]:
            return
        if not market_open(pos["market"], s, now):
            return
        long = pos["side"] == "long"
        sign = 1 if long else -1
        # Stop-loss
        if (long and price <= pos["stop"]) or (not long and price >= pos["stop"]):
            kind = "Winst-stop" if sign * (pos["stop"] - pos["entry_price"]) > 0 else "Stop-loss"
            self.close_position(run, pos, None, price, fx, f"{kind} geraakt (${fmt_px(pos['stop'])})", "regels", now)
            return
        # Doel 2
        if pos["target2"] and ((long and price >= pos["target2"]) or (not long and price <= pos["target2"])):
            self.close_position(run, pos, None, price, fx, f"Doel 2 gehaald (${fmt_px(pos['target2'])})", "regels", now)
            return
        # Maximale looptijd
        if s["max_hold_hours"] and now - parse_iso(pos["opened_at"]) >= timedelta(hours=s["max_hold_hours"]):
            self.close_position(run, pos, None, price, fx, f"Maximale looptijd van {s['max_hold_hours']} uur bereikt",
                                "regels", now)
            return
        new_stop = pos["stop"]
        # Doel 1: deel winst nemen + stop naar instap
        if not pos["t1_done"] and pos["target1"] and ((long and price >= pos["target1"]) or
                                                      (not long and price <= pos["target1"])):
            self.db.execute("UPDATE trading_positions SET t1_done=1 WHERE id=?", (pos["id"],))
            frac = s["take_profit_fraction"] / 100
            if frac >= 0.999:
                self.close_position(run, pos, None, price, fx, f"Doel 1 gehaald (${fmt_px(pos['target1'])})",
                                    "regels", now)
                return
            if frac > 0 and pos["qty"] * frac * price / fx >= s["min_order_eur"]:
                self.close_position(run, pos, pos["qty"] * frac, price, fx,
                                    f"Doel 1 gehaald (${fmt_px(pos['target1'])}): {frac * 100:.0f}% winst genomen",
                                    "regels", now)
                pos = self.db.query_one("SELECT * FROM trading_positions WHERE id=?", (pos["id"],))
                if not pos or pos["status"] != "open":
                    return
            pos["t1_done"] = 1
            if s["breakeven_after_t1"]:
                # 'Instapprijs' inclusief de kosten van in- en uitstappen, zodat de rest echt geen verlies wordt
                cost = 2 * (s["fee_pct_crypto" if pos["market"] == "crypto" else "fee_pct_stocks"]
                            + s["slippage_pct"]) / 100
                be = pos["entry_price"] * (1 + sign * cost)
                new_stop = max(new_stop, be) if long else min(new_stop, be)
        # Trailing stop na doel 1
        if pos["t1_done"] and s["trailing_atr_mult"] > 0 and pos.get("atr"):
            dist = s["trailing_atr_mult"] * pos["atr"]
            if long:
                new_stop = max(new_stop, (pos["high_water"] or price) - dist)
            else:
                new_stop = min(new_stop, (pos["low_water"] or price) + dist)
        if abs(new_stop - pos["stop"]) > EPS:
            self.db.execute("UPDATE trading_positions SET stop=? WHERE id=?", (new_stop, pos["id"]))
            self._move_exchange_stop(run, {**pos, "stop": new_stop}, new_stop, fx)

    # ------------------------------------------------------------------ beslismoment
    def _decision_due(self, run: dict, market: str, now: datetime) -> tuple[bool, str | None]:
        s, st = run["settings"], run["state"]
        last = parse_iso(st["last_decision"].get(market))
        if last is None or now - last >= timedelta(minutes=s["decision_minutes"]):
            return True, None
        if s["news_trigger"] and now - last >= MIN_TRIGGER_GAP:
            why = self._trigger(market, st["trigger_seen"].get(market) or iso(last))
            st["trigger_seen"][market] = iso(now)
            if why:
                return True, why
        return False, None

    def _trigger(self, market: str, since: str) -> str | None:
        a = self.db.query_one("SELECT title FROM alerts WHERE market=? AND status='verzonden' AND created_at > ?"
                              " ORDER BY id DESC LIMIT 1", (market, since))
        if a:
            return f"Noodmelding: {a['title']}"
        n = self.db.query_one("SELECT title FROM news WHERE market=? AND is_junk=0 AND kind != 'post' AND"
                              " corroboration >= 3 AND credibility >= 0.7 AND fetched_at > ? ORDER BY credibility DESC"
                              " LIMIT 1", (market, since))
        if n:
            return f"Groot nieuws: {n['title'][:120]}"
        p = self.db.query_one("SELECT author, title FROM news WHERE market=? AND is_junk=0 AND kind='post' AND"
                              " credibility >= 0.8 AND tickers != '[]' AND fetched_at > ? ORDER BY credibility DESC"
                              " LIMIT 1", (market, since))
        if p:
            return f"X-post van @{p['author']}: {p['title'][:100]}"
        return None

    def _entry_block(self, run: dict, market: str, now: datetime) -> str | None:
        s, st = run["settings"], run["state"]
        if run["capital"].get(market, 0) <= 0:
            return "geen budget voor deze markt"
        if not market_open(market, s, now):
            return "beurs gesloten"
        if st.get("paused"):
            return st["paused"]["reason"]
        if st["trades_today"].get(market, 0) >= s["max_trades_per_day"]:
            return f"maximaal {s['max_trades_per_day']} nieuwe trades per dag bereikt"
        return None

    def gate(self, run: dict, market: str, sig: dict, side: str, now: datetime, by_ai: bool = False) -> tuple:
        """Mag deze trade? Geeft (ok, code, reden, niveaus). De AI mag overtuiging en techniek-check overrulen."""
        s = run["settings"]
        broker = self.broker(run, market)
        if not by_ai and sig["conviction"] < s["min_conviction"]:
            return False, "conv", f"overtuiging {sig['conviction']} onder de grens {s['min_conviction']}", None
        if side == "short" and not (s["allow_short"] and broker.can_short):
            why = "shorten staat uit" if not s["allow_short"] else "short kan niet via de API van de broker (live)"
            return False, "short", f"Short-signaal, maar {why}", None
        if not by_ai and s["require_ta_confirmation"] and sig["ta_direction"] in ("long", "short") \
                and sig["ta_direction"] != side:
            return False, "ta", f"techniek wijst de andere kant op ({sig['ta_direction']})", None
        if sig.get("price_age_min") is None or sig["price_age_min"] > s["max_price_age_minutes"]:
            return False, "age", "koers te oud om op te handelen", None
        cd = run["state"]["cooldown"].get(f"{market}:{sig['ticker']}:{side}")
        if cd and cd > iso(now):
            return False, "cooldown", "pauze na een recente stop-loss", None
        lv = sig["levels"] if sig["side"] == side else levels_for(side, sig["price"], sig["plan"], s)
        if not lv or "skip" in lv:
            return False, "levels", (lv or {}).get("skip", "geen handelsplan"), None
        if lv["rr"] < s["min_rr"]:
            return False, "rr", f"risico/rendement {fmt_num(lv['rr'], 1)} onder de grens {fmt_num(s['min_rr'], 1)}", None
        if run["mode"] == "live" and not broker.supports(market, sig["ticker"]):
            return False, "broker", f"{sig['ticker']} is niet te verhandelen bij {broker.name}", None
        return True, "ok", "", lv

    def size(self, run: dict, market: str, sig: dict, lv: dict, conviction: float, size_factor: float, fx: float,
             port: dict) -> tuple[float, str]:
        """Positiegrootte in euro op basis van het risico per trade, begrensd door alle limieten."""
        s = run["settings"]
        m = port["markets"][market]
        base = m["equity"] if s["reinvest_profits"] else min(m["equity"], run["capital"][market])
        if base <= 0:
            return 0.0, "geen vermogen over"
        if len(m["positions"]) >= s["max_open_positions"]:
            return 0.0, f"al {len(m['positions'])} open posities (maximum)"
        conv_scale = 0.5 + 0.5 * min(conviction, 100) / 100
        risk_eur = base * s["risk_per_trade_pct"] / 100 * size_factor * conv_scale
        risk_unit_eur = lv["risk"] / fx
        price_eur = sig["price"] / fx
        value = risk_eur / risk_unit_eur * price_eur if risk_unit_eur > 0 else 0
        max_exp = min(s["max_exposure_pct"], 100.0) if run["mode"] == "live" else s["max_exposure_pct"]
        fee = s["fee_pct_crypto" if market == "crypto" else "fee_pct_stocks"] / 100
        caps = {"maximale positie": base * s["max_position_pct"] / 100,
                "maximale blootstelling": base * max_exp / 100 - m["exposure"]}
        if sig["side"] == "long" or lv.get("side") == "long":
            borrow = base * max(0.0, max_exp - 100) / 100
            caps["beschikbare kas"] = (m["cash"] + borrow) / (1 + fee)
        limit_name, limit = min(caps.items(), key=lambda kv: kv[1])
        if value > limit:
            value = limit
        if value < s["min_order_eur"]:
            reason = f"positie te klein ({fmt_eur(max(value, 0))} < minimum {fmt_eur(s['min_order_eur'])})"
            if limit < s["min_order_eur"]:
                reason += f": begrensd door {limit_name}"
            return 0.0, reason
        return value, ""

    # ------------------------------------------------------------------ beslissen per markt
    def decide(self, run: dict, market: str, now: datetime, fx: float, trigger: str | None = None) -> None:
        s, st = run["settings"], run["state"]
        st["last_decision"][market] = iso(now)
        positions = self.positions(run["id"], market)
        sigs = self.signals.evaluate(market, s, extra=[p["ticker"] for p in positions], now=now)
        by_t = {x["ticker"]: x for x in sigs}
        block = self._entry_block(run, market, now)
        if trigger:
            self.log(run["id"], market, None, "info", "info", "systeem", f"Direct beslissen: {trigger}")

        # Regels: posities waarvan het signaal is omgeslagen
        rule_closes = {}
        for pos in positions:
            sig = by_t.get(pos["ticker"])
            if sig and s["exit_on_flip"] and sig["side"] != pos["side"] and sig["conviction"] >= s["exit_on_flip"]:
                rule_closes[pos["ticker"]] = (f"Signaal omgeslagen naar {sig['side']} (overtuiging "
                                              f"{sig['conviction']})")
        # Regels: voorstellen voor nieuwe posities
        held = {p["ticker"] for p in positions}
        port = self.portfolio(run, fx, now)
        proposals = []
        for sig in sigs:
            if sig["ticker"] in held:
                continue
            ok, code, why, lv = self.gate(run, market, sig, sig["side"], now)
            if not ok:
                if sig["conviction"] >= s["min_conviction"]:
                    self._reject(run, market, sig, code, why, now=now)
                continue
            value, why2 = self.size(run, market, sig, lv, sig["conviction"], 1.0, fx, port)
            proposals.append({"sig": sig, "side": sig["side"], "lv": lv, "value": value, "size_note": why2})
        proposals.sort(key=lambda p: -(p["sig"]["conviction"] * min(p["lv"]["rr"], 4)))

        ai_result = self._ask_ai(run, market, now, fx, sigs, positions, proposals, rule_closes, port, block, trigger)
        if ai_result is None:
            self._apply_rules(run, market, now, fx, positions, proposals, rule_closes, block)
        else:
            self._apply_ai(run, market, now, fx, positions, proposals, rule_closes, by_t, ai_result, block)

    def _ai_status(self, run: dict, market: str, now: datetime, trigger: str | None) -> str:
        """'uit' (regels beslissen), 'limiet' (daglimiet: regels beslissen), 'wachten' (te kort na de vorige
        AI-beslissing: geen nieuwe trades) of 'ok'."""
        s, st = run["settings"], run["state"]
        if not (s["ai_enabled"] and self.ai.available):
            return "uit"
        if st.get("ai_day") != local_day(now):
            st["ai_day"], st["ai_calls_today"] = local_day(now), 0
        if st["ai_calls_today"] >= s["ai_max_calls_per_day"]:
            return "limiet"
        last = parse_iso(st["last_ai"].get(market))
        if last and now - last < timedelta(minutes=s["ai_min_interval_minutes"]) and not (
                trigger and now - last >= MIN_TRIGGER_GAP):
            return "wachten"
        return "ok"

    def _ask_ai(self, run, market, now, fx, sigs, positions, proposals, rule_closes, port, block, trigger):
        s, st = run["settings"], run["state"]
        interesting = [x for x in sigs if x["conviction"] >= s["min_conviction"] * 0.6]
        if not (proposals or positions or (s["ai_mode"] == "vrij" and interesting and not block)):
            return None
        status = self._ai_status(run, market, now, trigger)
        if status == "uit":
            return None
        if status == "limiet":
            key = (run["id"], market, "-", "ai_limit")
            if not self._rejections.get(key) or now - self._rejections[key] > REJECT_REPEAT:
                self._rejections[key] = now
                self.log(run["id"], market, None, "ai", "info", "systeem",
                         f"Maximum van {s['ai_max_calls_per_day']} AI-aanroepen vandaag bereikt: de regels beslissen.")
            return None
        if status == "wachten":
            # Tussen twee AI-momenten: alleen de regels voor bestaande posities, geen nieuwe trades
            return {"skip_new": True, "decisions": [], "market_view": None}
        m = port["markets"][market]
        recent = self.db.query(
            "SELECT ticker, side, pnl_eur, entry_price_eur, qty_initial, reason_close, closed_at FROM"
            " trading_positions WHERE run_id=? AND market=? AND status='closed' ORDER BY closed_at DESC LIMIT 5",
            (run["id"], market))
        ctx = {
            "market": market, "mode": run["mode"] + (" (proefdraaien)" if run["mode"] == "live" and s["live_dry_run"]
                                                     else ""),
            "now": iso(now), "settings": s, "can_short": bool(s["allow_short"] and self.broker(run, market).can_short),
            "signals": sigs, "market_view": st["market_view"].get(market),
            "portfolio": {
                "kas_eur": round(m["cash"], 2), "vermogen_eur": round(m["equity"], 2),
                "blootstelling_pct": round(m["exposure"] / m["equity"] * 100, 1) if m["equity"] > 0 else 0,
                "nieuwe_trades_mogelijk": block is None, "reden_geen_nieuwe_trades": block,
                "posities": [{"ticker": p["ticker"], "kant": p["side"], "instap_usd": round(p["entry_price"], 6),
                              "koers_usd": round(p["price"], 6), "resultaat_pct": round(p["upnl_pct"], 2),
                              "stop": round(p["stop"], 6), "doel1": round(p["target1"] or 0, 6),
                              "doel2": round(p["target2"] or 0, 6), "doel1_gehaald": bool(p["t1_done"]),
                              "looptijd_uur": round((now - parse_iso(p["opened_at"])).total_seconds() / 3600, 1),
                              "regel_wil_sluiten": rule_closes.get(p["ticker"])} for p in m["positions"]],
            },
            "proposals": [{"ticker": p["sig"]["ticker"], "kant": p["side"], "overtuiging": p["sig"]["conviction"],
                           "stop": round(p["lv"]["stop"], 6), "doel1": round(p["lv"]["target1"], 6),
                           "doel2": round(p["lv"]["target2"], 6), "risico_rendement": p["lv"]["rr"],
                           "voorgestelde_grootte_eur": round(p["value"], 2), "opmerking": p["size_note"] or None}
                          for p in proposals[:8]],
            "recent_trades": [{"ticker": r["ticker"], "kant": r["side"], "resultaat_eur": round(r["pnl_eur"] or 0, 2),
                               "resultaat_pct": round((r["pnl_eur"] or 0) / (r["entry_price_eur"] * r["qty_initial"])
                                                      * 100, 2) if r["qty_initial"] else 0,
                               "reden": r["reason_close"]} for r in recent],
        }
        try:
            result, usage = self.ai.decide(ctx)
        except Exception as exc:  # noqa: BLE001 - dan beslissen de regels
            self.log(run["id"], market, None, "ai", "fout", "ai", f"AI niet beschikbaar ({exc}): de regels beslissen.")
            return None
        st["last_ai"][market] = iso(now)
        st["ai_calls_today"] = st.get("ai_calls_today", 0) + 1
        st["market_view"][market] = result["market_view"]
        cost = usage.get("cost_usd", 0.0)
        self.db.execute("UPDATE trading_runs SET ai_calls=ai_calls+1, ai_cost_usd=ai_cost_usd+? WHERE id=?",
                        (cost, run["id"]))
        self.log(run["id"], market, None, "ai", "info", "ai", f"Marktvisie: {result['market_view']}",
                 details={"tokens_in": usage.get("input"), "tokens_out": usage.get("output"),
                          "kosten_usd": round(cost, 4), "model": usage.get("model")})
        return result

    def _apply_rules(self, run, market, now, fx, positions, proposals, rule_closes, block) -> None:
        for pos in positions:
            if pos["ticker"] in rule_closes:
                price, _ = self.quote(market, pos["ticker"], now)
                if price and market_open(market, run["settings"], now):
                    self.close_position(run, pos, None, price, fx, rule_closes[pos["ticker"]], "regels", now)
        if block:
            return
        for p in proposals:
            self._try_open(run, market, now, fx, p["sig"], p["side"], p["lv"], p["sig"]["conviction"], 1.0,
                           "Volgens de regels: " + self._why(p["sig"]), "regels")

    def _apply_ai(self, run, market, now, fx, positions, proposals, rule_closes, by_t, result, block) -> None:
        s = run["settings"]
        decisions = {d["ticker"]: d for d in result["decisions"]}
        open_market = market_open(market, s, now)
        # ---- open posities
        for pos in positions:
            d = decisions.get(pos["ticker"])
            price, _ = self.quote(market, pos["ticker"], now)
            if not price or not open_market:
                continue
            if d and d["action"] in ("close", "reduce"):
                q = None if d["action"] == "close" else pos["qty"] / 2
                self.close_position(run, pos, q, price, fx, f"AI: {d['reason']}", "ai", now)
            elif d and d["action"] == "tighten_stop" and d["stop"] > 0:
                ok = (pos["side"] == "long" and pos["stop"] < d["stop"] < price) or \
                     (pos["side"] == "short" and price < d["stop"] < pos["stop"])
                if ok:
                    self.db.execute("UPDATE trading_positions SET stop=? WHERE id=?", (d["stop"], pos["id"]))
                    self._move_exchange_stop(run, {**pos, "stop": d["stop"]}, d["stop"], fx)
                    self.log(run["id"], market, pos["ticker"], "stop", "uitgevoerd", "ai",
                             f"Stop verplaatst naar ${fmt_px(d['stop'])}: {d['reason']}")
            elif pos["ticker"] in rule_closes and not (d and d["action"] == "hold"):
                self.close_position(run, pos, None, price, fx, rule_closes[pos["ticker"]], "regels", now)
            elif pos["ticker"] in rule_closes and d:
                self.log(run["id"], market, pos["ticker"], "close", "afgewezen", "ai",
                         f"Regel wilde sluiten ({rule_closes[pos['ticker']]}), AI houdt vast: {d['reason']}")
        if result.get("skip_new") or block:
            return
        # ---- voorstellen van de regels
        prop_t = {p["sig"]["ticker"]: p for p in proposals}
        for p in proposals:
            d = decisions.get(p["sig"]["ticker"])
            if d and d["action"] == f"open_{p['side']}":
                lv = self._ai_levels(p["lv"], p["side"], p["sig"]["price"], d["stop"], s)
                self._try_open(run, market, now, fx, p["sig"], p["side"], lv, max(p["sig"]["conviction"],
                               d["confidence"]), d["size_factor"], f"AI: {d['reason']}", "ai")
            else:
                why = f"AI: {d['reason']}" if d else "Geen akkoord van de AI (niet genoemd)"
                self._reject(run, market, p["sig"], f"ai_{d['action'] if d else 'none'}", why, actor="ai", now=now)
        # ---- eigen keuzes van de AI (modus 'vrij')
        if s["ai_mode"] != "vrij":
            return
        held = {p["ticker"] for p in positions}
        for tk, d in decisions.items():
            if tk in prop_t or tk in held or d["action"] not in ("open_long", "open_short"):
                continue
            sig = by_t.get(tk)
            if not sig:
                self.log(run["id"], market, tk, d["action"], "afgewezen", "risico",
                         f"AI wilde {tk} openen, maar dat staat niet in de kandidatenlijst")
                continue
            side = d["action"].removeprefix("open_")
            ok, code, why, lv = self.gate(run, market, sig, side, now, by_ai=True)
            if not ok:
                self._reject(run, market, {**sig, "side": side}, f"ai_{code}", f"AI-keuze afgewezen: {why}",
                             actor="risico", now=now)
                continue
            lv = self._ai_levels(lv, side, sig["price"], d["stop"], s)
            self._try_open(run, market, now, fx, {**sig, "side": side}, side, lv, d["confidence"], d["size_factor"],
                           f"AI (eigen keuze): {d['reason']}", "ai")

    @staticmethod
    def _ai_levels(lv: dict, side: str, price: float, ai_stop: float, s: dict) -> dict:
        """Een stop van de AI gebruiken als die aan de goede kant ligt en redelijk ver weg is (0,5-4x ATR)."""
        if not ai_stop or ai_stop <= 0:
            return lv
        dist = (price - ai_stop) if side == "long" else (ai_stop - price)
        atr = lv.get("atr") or price * 0.02
        if not (0.5 * atr <= dist <= 4 * atr):
            return lv
        rr = abs(lv["target1"] - price) / dist
        if rr < s["min_rr"]:
            return lv
        return {**lv, "stop": ai_stop, "risk": dist, "rr": round(rr, 2)}

    def _try_open(self, run, market, now, fx, sig, side, lv, conviction, size_factor, reason, actor) -> None:
        if self._entry_block(run, market, now):
            return
        if self.positions_count(run["id"], market, sig["ticker"]):
            return
        port = self.portfolio(run, fx, now)
        value, why = self.size(run, market, {**sig, "side": side}, {**lv, "side": side}, conviction, size_factor,
                               fx, port)
        if value <= 0:
            self._reject(run, market, {**sig, "side": side}, "size", why, actor="risico", now=now)
            return
        if run["mode"] == "live" and side == "long":
            try:
                free = self.broker(run, market).free_cash_eur(market)
            except BrokerError as exc:
                self.log(run["id"], market, sig["ticker"], f"open_{side}", "fout", "systeem",
                         f"Saldo opvragen mislukt: {exc}")
                return
            if free is not None and value > free * 0.99:
                if free * 0.99 < run["settings"]["min_order_eur"]:
                    self._reject(run, market, {**sig, "side": side}, "cash",
                                 f"te weinig saldo bij de broker ({fmt_eur(free)})", actor="risico", now=now)
                    return
                value = free * 0.99
        self.open_position(run, market, sig, side, lv, value, fx, reason, actor, conviction, now)

    def positions_count(self, run_id: int, market: str, ticker: str) -> int:
        return self.db.query_one("SELECT COUNT(*) n FROM trading_positions WHERE run_id=? AND market=? AND ticker=?"
                                 " AND status='open'", (run_id, market, ticker))["n"]

    @staticmethod
    def _why(sig: dict) -> str:
        c = sig["components"]
        parts = [f"techniek {c['ta']:+.2f}"]
        for k, label in (("flow", "kopers"), ("news", "nieuws"), ("x", "X")):
            if abs(c[k]) > 0.01:
                parts.append(f"{label} {c[k]:+.2f}")
        return f"overtuiging {sig['conviction']} ({', '.join(parts)})".replace(".", ",")

    # ================================================================== vastleggen en rapporteren
    def snapshot(self, run: dict, now: datetime, force: bool = False) -> None:
        last = self.db.query_one("SELECT MAX(ts) ts FROM trading_equity WHERE run_id=?", (run["id"],))["ts"]
        gap = timedelta(seconds=55 if run["mode"] == "paper" else 175)
        if not force and last and now - parse_iso(last) < gap:
            return
        port = self.portfolio(run, self.fx.eurusd(), now)
        self.db.execute("INSERT OR REPLACE INTO trading_equity(run_id, ts, equity, cash, exposure) VALUES (?,?,?,?,?)",
                        (run["id"], iso(now), round(port["equity"], 4), round(port["cash"], 4),
                         round(port["exposure"], 4)))

    def stats(self, run_id: int) -> dict:
        run = self.get_run(run_id)
        start = sum(run["capital"].values())
        now = self.clock()
        port = self.portfolio(run, self.fx.eurusd(), now)
        equity = port["equity"]
        eq_rows = self.db.query("SELECT equity FROM trading_equity WHERE run_id=? ORDER BY ts", (run_id,))
        peak, max_dd = start, 0.0
        for r in eq_rows + [{"equity": equity}]:
            peak = max(peak, r["equity"])
            if peak > 0:
                max_dd = max(max_dd, 1 - r["equity"] / peak)
        closed = self.db.query("SELECT pnl_eur, market FROM trading_positions WHERE run_id=? AND status='closed'",
                               (run_id,))
        wins = [c["pnl_eur"] for c in closed if (c["pnl_eur"] or 0) > 0]
        losses = [c["pnl_eur"] for c in closed if (c["pnl_eur"] or 0) <= 0]
        fees = self.db.query_one("SELECT COALESCE(SUM(fee_eur),0) f FROM trading_fills WHERE run_id=?", (run_id,))["f"]
        n_fills = self.db.query_one("SELECT COUNT(*) n FROM trading_fills WHERE run_id=?", (run_id,))["n"]
        end = parse_iso(run.get("ended_at")) or now
        hours = (end - parse_iso(run["created_at"])).total_seconds() / 3600
        per_market = {}
        for m, cap in run["capital"].items():
            if cap > 0:
                eq = port["markets"][m]["equity"]
                per_market[m] = {"capital": cap, "equity": round(eq, 2), "return_pct": round((eq / cap - 1) * 100, 2)}
        return {
            "start_eur": round(start, 2),
            "equity_eur": round(equity, 2),
            "pnl_eur": round(equity - start, 2),
            "return_pct": round((equity / start - 1) * 100, 2) if start else 0.0,
            "max_drawdown_pct": round(max_dd * 100, 2),
            "trades": len(closed),
            "orders": n_fills,
            "open_positions": sum(len(v["positions"]) for v in port["markets"].values()),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
            "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) < 0 else None,
            "avg_win_eur": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss_eur": round(sum(losses) / len(losses), 2) if losses else None,
            "best_eur": round(max((c["pnl_eur"] or 0) for c in closed), 2) if closed else None,
            "worst_eur": round(min((c["pnl_eur"] or 0) for c in closed), 2) if closed else None,
            "fees_eur": round(fees, 2),
            "exposure_eur": round(port["exposure"], 2),
            "hours": round(hours, 2),
            "ai_calls": run.get("ai_calls") or 0,
            "ai_cost_usd": round(run.get("ai_cost_usd") or 0, 3),
            "markets": per_market,
        }

    def run_summary(self, run: dict) -> dict:
        st = self.stats(run["id"]) if run["status"] == "running" or not run.get("stats") else run["stats"]
        s = run["settings"]
        return {"id": run["id"], "name": run["name"], "mode": run["mode"], "status": run["status"],
                "dry_run": run["mode"] == "live" and s.get("live_dry_run"), "created_at": run["created_at"],
                "ends_at": run.get("ends_at"), "ended_at": run.get("ended_at"), "halt_reason": run.get("halt_reason"),
                "paused": (run["state"].get("paused") or {}).get("reason") if run["status"] == "running" else None,
                "stats": st,
                "key_settings": {k: s.get(k) for k in ("trade_style", "risk_per_trade_pct", "max_position_pct",
                                                       "min_conviction", "min_rr", "allow_short", "ai_enabled",
                                                       "ai_mode", "ai_model", "weight_ta", "weight_flow",
                                                       "weight_news", "weight_x", "decision_minutes")}}

    def detail(self, run_id: int) -> dict | None:
        run = self.get_run(run_id)
        if not run:
            return None
        now = self.clock()
        fx = self.fx.eurusd()
        port = self.portfolio(run, fx, now)
        positions = []
        for m in ("stocks", "crypto"):
            for p in port["markets"][m]["positions"]:
                positions.append({k: p.get(k) for k in ("id", "market", "ticker", "side", "qty", "entry_price",
                                                        "price", "value_eur", "upnl_eur", "upnl_pct", "stop",
                                                        "target1", "target2", "t1_done", "opened_at", "source",
                                                        "conviction", "reason_open", "pnl_eur")})
        closed = self.db.query("SELECT * FROM trading_positions WHERE run_id=? AND status='closed' ORDER BY closed_at"
                               " DESC LIMIT 100", (run_id,))
        for c in closed:
            c["pnl_pct"] = round((c["pnl_eur"] or 0) / (c["entry_price_eur"] * c["qty_initial"]) * 100, 2) \
                if c["qty_initial"] else None
            c.pop("details", None)
        eq = self.db.query("SELECT ts, equity FROM trading_equity WHERE run_id=? ORDER BY ts", (run_id,))
        if len(eq) > 400:
            step = math.ceil(len(eq) / 400)
            eq = eq[::step] + [eq[-1]]
        decisions = self.db.query("SELECT * FROM trading_decisions WHERE run_id=? ORDER BY id DESC LIMIT 200",
                                  (run_id,))
        for d in decisions:
            d["details"] = json.loads(d["details"]) if d.get("details") else None
        return {**self.run_summary(run), "settings": run["settings"], "capital": run["capital"],
                "market_view": run["state"].get("market_view", {}), "positions": positions, "closed": closed,
                "equity": eq, "decisions": decisions, "fx": fx,
                "cash": {m: round(port["markets"][m]["cash"], 2) for m in ("stocks", "crypto")}}

    def publish(self, now: datetime) -> None:
        """Sensor in Home Assistant (elke 5 minuten)."""
        if not self.hass or not getattr(self.hass, "enabled", False):
            return
        if self._last_publish and now - self._last_publish < timedelta(minutes=5):
            return
        self._last_publish = now
        runs = [self.run_summary(r) for r in self.running_runs()]
        live = next((r for r in runs if r["mode"] == "live"), None)
        state = f"{live['stats']['equity_eur']:.2f}" if live else "geen live-run"
        self.hass.set_state("sensor.market_monitor_trading", state, {
            "friendly_name": "Market Monitor: handelen",
            "icon": "mdi:robot",
            "unit_of_measurement": "EUR" if live else None,
            "live_resultaat_pct": live["stats"]["return_pct"] if live else None,
            "live_open_posities": live["stats"]["open_positions"] if live else None,
            "runs": [{"naam": r["name"], "modus": r["mode"], "resultaat_pct": r["stats"]["return_pct"],
                      "vermogen_eur": r["stats"]["equity_eur"]} for r in runs],
        })
