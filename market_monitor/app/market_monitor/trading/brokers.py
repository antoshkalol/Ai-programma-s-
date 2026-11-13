"""Brokers: paper (gesimuleerd), Bitvavo (crypto) en Trading 212 (aandelen).

Alle brokers krijgen dezelfde opdracht (Order) en geven dezelfde uitvoering (Fill) terug. Koersen in het dashboard
zijn in dollar; het budget en de boekhouding in euro (omgerekend met de ECB-koers 'fx' = dollars per euro).

Veiligheid:
  * Orders worden NOOIT automatisch opnieuw verstuurd (geen retries op POST): een time-out zou anders een dubbele
    order kunnen geven. Bij twijfel stopt de run en krijg je een melding.
  * Live kan alleen long: de API van Trading 212 (Invest-account) en van Bitvavo ondersteunen geen short/marge.
  * 'Proefdraaien' (dry-run): saldo wordt echt gelezen, orders worden alleen gelogd en tegen de laatste koers
    gesimuleerd.
  * Gebruik API-sleutels zonder opnamerechten en zet bij de broker een IP-whitelist aan.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode, urlparse

import requests

from ..db import iso, parse_iso, utcnow
from ..net import DEFAULT_UA, _limiter

log = logging.getLogger(__name__)


class BrokerError(Exception):
    """Order of verzoek mislukt. 'uncertain' = onbekend of de order is uitgevoerd (bv. time-out)."""

    def __init__(self, message: str, uncertain: bool = False):
        super().__init__(message)
        self.uncertain = uncertain


@dataclass
class Order:
    market: str            # stocks | crypto
    ticker: str
    side: str              # buy | sell
    ref_price: float       # laatste koers in dollar
    fx: float              # dollars per euro
    qty: float | None = None
    eur: float | None = None
    intent: str = ""       # open_long | close_long | open_short | close_short


def fill_dict(qty: float, price: float, fx: float, fee_eur: float, order_id: str | None = None,
              dry_run: bool = False, note: str = "") -> dict:
    price_eur = price / fx
    return {"qty": qty, "price": price, "price_eur": price_eur, "value_eur": qty * price_eur, "fee_eur": fee_eur,
            "order_id": order_id, "dry_run": dry_run, "note": note}


def floor_to(x: float, decimals: int) -> float:
    f = 10 ** decimals
    return math.floor(x * f + 1e-9) / f


def fmt_sig(x: float, digits: int = 5) -> str:
    """Getal met 'digits' significante cijfers, zonder e-notatie (Bitvavo-prijsprecisie)."""
    if x <= 0:
        return "0"
    decimals = max(0, digits - 1 - int(math.floor(math.log10(x))))
    v = round(x, decimals)
    return f"{v:.{decimals}f}".rstrip("0").rstrip(".") if decimals else str(int(v))


def fmt_qty(x: float, decimals: int) -> str:
    v = floor_to(x, decimals)
    return f"{v:.{decimals}f}".rstrip("0").rstrip(".") if decimals else str(int(v))


# --------------------------------------------------------------------------- paper
class PaperBroker:
    name = "paper"
    live = False
    can_short = True

    def __init__(self, settings: dict):
        self.settings = settings

    def fee_pct(self, market: str) -> float:
        key = "fee_pct_crypto" if market == "crypto" else "fee_pct_stocks"
        return self.settings[key] / 100

    def simulate(self, order: Order, note: str = "", dry_run: bool = False) -> dict:
        slip = self.settings["slippage_pct"] / 100
        price = order.ref_price * (1 + slip if order.side == "buy" else 1 - slip)
        qty = order.qty if order.qty is not None else (order.eur or 0) / (price / order.fx)
        fee = qty * price / order.fx * self.fee_pct(order.market)
        return fill_dict(qty, price, order.fx, fee, order_id=f"sim-{uuid.uuid4().hex[:10]}", dry_run=dry_run,
                         note=note)

    def execute(self, order: Order) -> dict:
        return self.simulate(order)

    def free_cash_eur(self, market: str) -> float | None:
        return None

    def supports(self, market: str, ticker: str) -> bool:
        return True


# --------------------------------------------------------------------------- basis voor live
class LiveBroker:
    live = True
    can_short = False
    market = ""

    def __init__(self, settings: dict, dry_run: bool, session: requests.Session | None = None, sleep=time.sleep):
        self.settings = settings
        self.dry_run = dry_run
        self.session = session or requests.Session()     # bewust zonder automatische retries
        self.session.headers.setdefault("User-Agent", DEFAULT_UA)
        self.sleep = sleep
        self.paper = PaperBroker(settings)

    def _send(self, method: str, url: str, headers: dict, body: str | None = None, timeout: float = 15):
        _limiter.wait(urlparse(url).hostname or "")
        try:
            return self.session.request(method, url, headers=headers, data=body, timeout=timeout)
        except requests.Timeout as exc:
            raise BrokerError(f"{self.name}: time-out bij {method} {urlparse(url).path}",
                              uncertain=method != "GET") from exc
        except requests.RequestException as exc:
            raise BrokerError(f"{self.name}: niet bereikbaar ({exc.__class__.__name__})",
                              uncertain=False) from exc

    def execute(self, order: Order) -> dict:
        if order.intent == "open_short" or order.intent == "close_short":
            raise BrokerError(f"{self.name}: short gaan kan niet via de API")
        if self.dry_run:
            log.info("PROEFDRAAIEN %s: zou %s %s %s sturen", self.name, order.side, order.ticker,
                     f"{order.qty:.6g} stuks" if order.qty else f"€{order.eur:.2f}")
            return self.paper.simulate(order, note="proefdraaien: geen echte order", dry_run=True)
        return self.place(order)

    def place(self, order: Order) -> dict:  # pragma: no cover - per broker
        raise NotImplementedError


# --------------------------------------------------------------------------- Bitvavo
class BitvavoBroker(LiveBroker):
    """Bitvavo REST API v2. Handtekening: HMAC-SHA256(secret, timestamp + methode + '/v2' + pad + body)."""

    name = "Bitvavo"
    market = "crypto"
    BASE = "https://api.bitvavo.com/v2"

    def __init__(self, key: str, secret: str, settings: dict, dry_run: bool, session=None, sleep=time.sleep):
        super().__init__(settings, dry_run, session, sleep)
        self.key, self.secret = key.strip(), secret.strip()
        self._markets: dict[str, dict] = {}

    def signature(self, ts: str, method: str, path: str, body: str = "") -> str:
        msg = f"{ts}{method}/v2{path}{body}"
        return hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

    def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
                auth: bool = True):
        if params:
            path = f"{path}?{urlencode(params)}"
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        headers = {"Content-Type": "application/json"}
        if auth:
            ts = str(int(time.time() * 1000))
            headers.update({
                "Bitvavo-Access-Key": self.key,
                "Bitvavo-Access-Signature": self.signature(ts, method, path, payload),
                "Bitvavo-Access-Timestamp": ts,
                "Bitvavo-Access-Window": "10000",
            })
        r = self._send(method, self.BASE + path, headers, payload or None)
        try:
            data = r.json()
        except ValueError:
            data = {"error": r.text[:200]}
        if r.status_code >= 400 or (isinstance(data, dict) and data.get("errorCode")):
            msg = data.get("error") if isinstance(data, dict) else str(data)
            raise BrokerError(f"Bitvavo {r.status_code}: {msg}")
        return data

    def market_info(self, ticker: str) -> dict:
        m = f"{ticker}-EUR"
        if m not in self._markets:
            data = self.request("GET", "/markets", {"market": m}, auth=False)
            self._markets[m] = data if isinstance(data, dict) else (data[0] if data else {})
        return self._markets[m]

    def supports(self, market: str, ticker: str) -> bool:
        if market != "crypto":
            return False
        try:
            return self.market_info(ticker).get("status") == "trading"
        except BrokerError:
            return False

    def free_cash_eur(self, market: str = "crypto") -> float | None:
        data = self.request("GET", "/balance", {"symbol": "EUR"})
        row = data[0] if isinstance(data, list) and data else {}
        return float(row.get("available", 0) or 0)

    def balance(self, symbol: str) -> float:
        data = self.request("GET", "/balance", {"symbol": symbol})
        row = data[0] if isinstance(data, list) and data else {}
        return float(row.get("available", 0) or 0)

    def _qty_decimals(self, info: dict) -> int:
        if info.get("quantityDecimals") is not None:
            return int(info["quantityDecimals"])
        return 8

    def _price_str(self, info: dict, price_eur: float) -> str:
        tick = info.get("tickSize")
        if tick:
            t = float(tick)
            decimals = max(0, -int(math.floor(math.log10(t)))) if t < 1 else 0
            return f"{math.floor(price_eur / t) * t:.{decimals}f}"
        return fmt_sig(price_eur, int(info.get("pricePrecision") or 5))

    def place(self, order: Order) -> dict:
        info = self.market_info(order.ticker)
        body: dict = {"market": f"{order.ticker}-EUR", "side": order.side, "orderType": "market",
                      "clientOrderId": str(uuid.uuid4()), "operatorId": int(self.settings["bitvavo_operator_id"])}
        if order.side == "buy":
            eur = order.eur if order.eur is not None else (order.qty or 0) * order.ref_price / order.fx
            body["amountQuote"] = f"{math.floor(eur * 100) / 100:.2f}"
        else:
            # Nooit meer verkopen dan er vrij beschikbaar is (kosten in de munt maken het saldo soms iets kleiner)
            qty = min(order.qty or 0, self.balance(order.ticker))
            body["amount"] = fmt_qty(qty, self._qty_decimals(info))
            if float(body["amount"]) <= 0:
                raise BrokerError(f"Bitvavo: geen {order.ticker} beschikbaar om te verkopen")
        data = self.request("POST", "/order", body=body)
        if data.get("status") not in ("filled", "cancelled", "expired", "rejected"):
            for _ in range(5):
                self.sleep(1.0)
                data = self.request("GET", "/order", {"market": body["market"], "orderId": data.get("orderId")})
                if data.get("status") in ("filled", "cancelled", "expired", "rejected"):
                    break
        return self._fill_from(order, data)

    def _fill_from(self, order: Order, data: dict) -> dict:
        qty = float(data.get("filledAmount") or 0)
        quote = float(data.get("filledAmountQuote") or 0)
        if qty <= 0:
            raise BrokerError(f"Bitvavo: order niet uitgevoerd (status {data.get('status')})")
        price_eur = quote / qty
        fee = float(data.get("feePaid") or 0)
        fee_cur = (data.get("feeCurrency") or "EUR").upper()
        if fee_cur == order.ticker.upper():
            fee_eur = fee * price_eur
            if order.side == "buy":
                qty -= fee          # kosten in de munt zelf: je krijgt iets minder munten
        elif fee_cur == "EUR":
            fee_eur = fee
        else:
            fee_eur = 0.0
        return fill_dict(qty, price_eur * order.fx, order.fx, fee_eur, order_id=data.get("orderId"),
                         note=f"Bitvavo {data.get('status')}")

    # ---------------------------------------------------------------- stop-loss op de beurs
    def place_stop(self, ticker: str, qty: float, stop_usd: float, fx: float) -> str | None:
        if self.dry_run:
            return None
        info = self.market_info(ticker)
        body = {"market": f"{ticker}-EUR", "side": "sell", "orderType": "stopLoss",
                "amount": fmt_qty(qty, self._qty_decimals(info)),
                "triggerAmount": self._price_str(info, stop_usd / fx), "triggerType": "price",
                "triggerReference": "lastTrade", "clientOrderId": str(uuid.uuid4()),
                "operatorId": int(self.settings["bitvavo_operator_id"])}
        data = self.request("POST", "/order", body=body)
        return data.get("orderId")

    def cancel_order(self, ticker: str, order_id: str) -> None:
        if self.dry_run or not order_id:
            return
        try:
            self.request("DELETE", "/order", {"market": f"{ticker}-EUR", "orderId": order_id,
                                              "operatorId": int(self.settings["bitvavo_operator_id"])})
        except BrokerError as exc:
            # Al uitgevoerd of al geannuleerd: niet erg
            log.info("Bitvavo stop-order %s annuleren: %s", order_id, exc)

    def order_status(self, ticker: str, order_id: str) -> dict:
        return self.request("GET", "/order", {"market": f"{ticker}-EUR", "orderId": order_id})


# --------------------------------------------------------------------------- Trading 212
class Trading212Broker(LiveBroker):
    """Trading 212 Public API v0 (Invest-account). Alleen marktorders; verkopen = negatieve hoeveelheid."""

    name = "Trading 212"
    market = "stocks"

    def __init__(self, key: str, secret: str, settings: dict, dry_run: bool, db=None, session=None,
                 sleep=time.sleep):
        super().__init__(settings, dry_run, session, sleep)
        self.key, self.secret = key.strip(), secret.strip()
        self.db = db
        env = settings.get("trading212_env", "demo")
        self.base = f"https://{'live' if env == 'live' else 'demo'}.trading212.com/api/v0"
        self._instruments: dict[str, str] | None = None

    def auth_header(self) -> str:
        if self.secret:
            token = base64.b64encode(f"{self.key}:{self.secret}".encode()).decode()
            return f"Basic {token}"
        return self.key      # oudere sleutels: alleen de sleutel als header

    def request(self, method: str, path: str, body: dict | None = None):
        headers = {"Authorization": self.auth_header(), "Content-Type": "application/json"}
        r = self._send(method, self.base + path, headers, json.dumps(body) if body is not None else None)
        if r.status_code == 404:
            return None
        try:
            data = r.json() if r.text else {}
        except ValueError:
            data = {"error": r.text[:200]}
        if r.status_code >= 400:
            code = (data.get("code") or data.get("errorMessage") or data.get("error") or "") if isinstance(data, dict) \
                else str(data)
            if r.status_code in (401, 403):
                code = "sleutel ongeldig of zonder rechten (of IP niet toegestaan)"
            if r.status_code == 429:
                code = "te veel verzoeken"
            raise BrokerError(f"Trading 212 {r.status_code}: {code}")
        return data

    def instruments(self) -> dict[str, str]:
        """Ticker (bv. AAPL) -> Trading 212-ticker (bv. AAPL_US_EQ). Eén keer per dag ververst."""
        if self._instruments is not None:
            return self._instruments
        cached = self.db.get_state("t212_instruments") if self.db else None
        if cached and parse_iso(cached.get("at")) and utcnow() - parse_iso(cached["at"]) < timedelta(days=1):
            self._instruments = cached["map"]
            return self._instruments
        data = self.request("GET", "/equity/metadata/instruments") or []
        mapping: dict[str, str] = {}
        for ins in data:
            short, t212 = str(ins.get("shortName") or "").upper(), ins.get("ticker")
            if not short or not t212 or ins.get("currencyCode") != "USD":
                continue
            if short not in mapping or t212.endswith("_US_EQ"):
                mapping[short] = t212
        if mapping:
            self._instruments = mapping
            if self.db:
                self.db.set_state("t212_instruments", {"at": iso(utcnow()), "map": mapping})
        return mapping

    def instrument(self, ticker: str) -> str:
        try:
            return self.instruments().get(ticker.upper()) or f"{ticker.upper().replace('.', '_')}_US_EQ"
        except BrokerError:
            return f"{ticker.upper().replace('.', '_')}_US_EQ"

    def supports(self, market: str, ticker: str) -> bool:
        if market != "stocks":
            return False
        try:
            inst = self.instruments()
        except BrokerError:
            return True
        return not inst or ticker.upper() in inst

    def free_cash_eur(self, market: str = "stocks") -> float | None:
        data = self.request("GET", "/equity/account/cash")
        if isinstance(data, dict) and data.get("free") is not None:
            return float(data["free"])
        data = self.request("GET", "/equity/account/summary") or {}
        cash = data.get("cash") if isinstance(data, dict) else None
        if isinstance(cash, dict):
            for k in ("availableToTrade", "free", "available"):
                if cash.get(k) is not None:
                    return float(cash[k])
        return None

    def place(self, order: Order) -> dict:
        qty = order.qty if order.qty is not None else (order.eur or 0) * order.fx / order.ref_price
        ticker = self.instrument(order.ticker)
        last_err = None
        for decimals in (4, 2, 0):
            q = floor_to(qty, decimals)
            if q <= 0:
                continue
            try:
                data = self.request("POST", "/equity/orders/market",
                                    {"ticker": ticker, "quantity": -q if order.side == "sell" else q})
            except BrokerError as exc:
                last_err = exc
                text = str(exc).lower()
                if exc.uncertain or not any(w in text for w in ("quantity", "precision", "fraction")):
                    raise
                continue
            return self._confirm(order, data or {}, q)
        raise last_err or BrokerError("Trading 212: hoeveelheid te klein")

    def _confirm(self, order: Order, data: dict, q: float) -> dict:
        oid = data.get("id")
        info = data
        for _ in range(5):
            if str(info.get("status", "")).upper() in ("FILLED", "CANCELLED", "REJECTED"):
                break
            self.sleep(1.5)
            nxt = self.request("GET", f"/equity/orders/{oid}") if oid is not None else None
            if nxt is None:           # niet meer openstaand: uitgevoerd
                info = {**info, "status": "FILLED"}
                break
            info = nxt
        status = str(info.get("status", "")).upper()
        if status in ("CANCELLED", "REJECTED"):
            raise BrokerError(f"Trading 212: order {status.lower()}")
        filled_q = float(info.get("filledQuantity") or q)
        filled_v = info.get("filledValue")
        price = abs(float(filled_v)) / filled_q if filled_v and filled_q else order.ref_price
        note = "Trading 212 uitgevoerd" if status == "FILLED" else "Trading 212: uitvoering niet bevestigd"
        fee = abs(filled_q) * price / order.fx * self.settings["fee_pct_stocks"] / 100
        return fill_dict(abs(filled_q), price, order.fx, fee, order_id=str(oid) if oid is not None else None, note=note)
