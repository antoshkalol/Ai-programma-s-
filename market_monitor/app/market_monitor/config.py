"""Configuratie.

In Home Assistant staan de add-on opties in /data/options.json. Lokaal (voor
ontwikkeling) wordt <data-dir>/options.json gelezen. Ontbrekende waarden vallen
terug op de standaardwaarden hieronder.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)

MARKETS = ("stocks", "crypto")
MARKET_LABELS = {"stocks": "Aandelen", "crypto": "Crypto"}

# Modellen die de server-side fallback ("fallbacks": "default") ondersteunen.
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
SENSITIVITY = {"laag": 1.5, "normaal": 1.0, "hoog": 0.7}


@dataclass
class Config:
    # Verplicht door de SEC: "Naam e-mailadres" (wordt alleen naar sec.gov gestuurd).
    sec_user_agent: str = ""

    # AI-samenvatting (optioneel). Zonder sleutel maakt de app een automatische samenvatting.
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: str = "medium"
    summary_language: str = "nl"
    summary_interval_minutes: int = 60

    # ---------------------------------------------------------------- Aandelen
    enable_stocks: bool = True
    stock_watchlist: list[str] = field(
        default_factory=lambda: ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "JPM", "ASML"]
    )
    stock_news_queries: list[str] = field(
        default_factory=lambda: ["stock market", "earnings", "insider buying", "Federal Reserve", "Wall Street"]
    )
    stock_x_accounts: list[str] = field(
        default_factory=lambda: [
            "elonmusk", "realDonaldTrump", "WhiteHouse", "federalreserve", "SECGov", "Reuters", "business",
            "WSJmarkets", "CNBC", "DeItaone",
        ]
    )
    enable_sec_form4: bool = True
    enable_house: bool = True
    enable_senate: bool = True
    quiver_api_key: str = ""
    stock_price_api_key: str = ""     # optioneel: gratis Twelve Data-sleutel voor betrouwbare koersen
    min_trade_value_usd: int = 100_000
    big_trade_alert_usd: int = 1_000_000

    # ---------------------------------------------------------------- Crypto
    enable_crypto: bool = True
    crypto_watchlist: list[str] = field(
        default_factory=lambda: ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "LINK", "AVAX", "SUI"]
    )
    crypto_news_queries: list[str] = field(
        default_factory=lambda: ["bitcoin", "ethereum", "crypto market", "crypto ETF", "SEC crypto"]
    )
    crypto_x_accounts: list[str] = field(
        default_factory=lambda: [
            "saylor", "VitalikButerin", "brian_armstrong", "cz_binance", "EricBalchunas", "coindesk", "TheBlock__",
            "tier10k", "glassnode", "WhaleAlert",
        ]
    )
    enable_crypto_treasury: bool = True
    enable_btc_whales: bool = True
    crypto_whale_min_usd: int = 50_000_000
    coingecko_api_key: str = ""

    # ---------------------------------------------------------------- Nieuws (gedeeld)
    enable_rss: bool = True
    enable_google_news: bool = True
    enable_gdelt: bool = True
    extra_rss_feeds: list[str] = field(default_factory=list)
    extra_crypto_rss_feeds: list[str] = field(default_factory=list)
    trusted_domains: list[str] = field(default_factory=list)
    blocked_domains: list[str] = field(default_factory=list)
    min_news_credibility: float = 0.45
    x_bearer_token: str = ""

    # ---------------------------------------------------------------- Meldingen
    notify_service: str = ""            # leeg = automatisch alle telefoons met de HA-app; of bv. mobile_app_iphone
    notify_summaries: bool = True       # samenvatting elk uur als pushbericht
    quiet_hours: str = ""               # bv. "23-7": dan geen uurbericht (noodmeldingen komen wel)
    alerts_enabled: bool = True
    alert_sensitivity: str = "normaal"  # laag | normaal | hoog
    alert_critical: bool = True         # noodmelding doorbreekt stil/niet-storen (iOS critical, Android alarm)
    ai_check_alerts: bool = True        # laat Claude een noodmelding eerst controleren (met API-sleutel)
    max_alerts_per_day: int = 6

    # Home Assistant
    publish_to_home_assistant: bool = True

    log_level: str = "info"

    # Niet via opties: runtime
    data_dir: str = "/data"
    port: int = 8099

    # ------------------------------------------------------------------
    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key.strip())

    @property
    def model_supports_fallback(self) -> bool:
        return self.anthropic_model in FALLBACK_MODELS

    @property
    def sec_headers_ok(self) -> bool:
        return "@" in self.sec_user_agent

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "market_monitor.db"

    @property
    def sensitivity(self) -> float:
        """Vermenigvuldiger voor alert-drempels: laag = strenger (hoger), hoog = gevoeliger."""
        return SENSITIVITY.get(self.alert_sensitivity, 1.0)

    @property
    def markets(self) -> list[str]:
        out = []
        if self.enable_stocks:
            out.append("stocks")
        if self.enable_crypto:
            out.append("crypto")
        return out

    def watchlist(self, market: str) -> list[str]:
        return self.crypto_watchlist if market == "crypto" else self.stock_watchlist

    def news_queries(self, market: str) -> list[str]:
        return self.crypto_news_queries if market == "crypto" else self.stock_news_queries

    def x_accounts(self, market: str) -> list[str]:
        return self.crypto_x_accounts if market == "crypto" else self.stock_x_accounts

    def public_dict(self) -> dict:
        """Configuratie zonder geheimen, voor het dashboard."""
        d = asdict(self)
        for secret in ("anthropic_api_key", "quiver_api_key", "x_bearer_token", "coingecko_api_key",
                       "stock_price_api_key"):
            d[secret] = bool(d[secret])
        d["sec_user_agent"] = bool(self.sec_headers_ok)
        d["markets"] = self.markets
        return d


def _coerce(value, default):
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, list):
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return default
    return "" if value is None else str(value)


# Oude optienamen (v0.1) blijven werken
RENAMED = {"watchlist": "stock_watchlist", "news_queries": "stock_news_queries", "x_accounts": "stock_x_accounts"}


def load_config(data_dir: str = "/data", port: int = 8099) -> Config:
    cfg = Config(data_dir=data_dir, port=port)
    candidates = [Path(os.environ.get("MM_OPTIONS", "")), Path(data_dir) / "options.json"]
    raw: dict = {}
    for path in candidates:
        if path and path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                log.info("Opties geladen uit %s", path)
                break
            except (OSError, json.JSONDecodeError) as exc:
                log.error("Kon opties niet lezen uit %s: %s", path, exc)

    for old, new in RENAMED.items():
        if old in raw and new not in raw:
            raw[new] = raw[old]

    for f in fields(Config):
        if f.name in ("data_dir", "port"):
            continue
        if f.name in raw:
            setattr(cfg, f.name, _coerce(raw[f.name], getattr(cfg, f.name)))

    # Omgevingsvariabelen (handig bij lokaal testen)
    env_map = {
        "ANTHROPIC_API_KEY": "anthropic_api_key",
        "MM_SEC_USER_AGENT": "sec_user_agent",
        "MM_QUIVER_API_KEY": "quiver_api_key",
        "MM_X_BEARER_TOKEN": "x_bearer_token",
        "MM_COINGECKO_API_KEY": "coingecko_api_key",
        "MM_STOCK_PRICE_API_KEY": "stock_price_api_key",
    }
    for env, attr in env_map.items():
        if os.environ.get(env) and not getattr(cfg, attr):
            setattr(cfg, attr, os.environ[env])

    cfg.stock_watchlist = [t.upper() for t in cfg.stock_watchlist]
    cfg.crypto_watchlist = [t.upper() for t in cfg.crypto_watchlist]
    cfg.stock_x_accounts = [a.lstrip("@") for a in cfg.stock_x_accounts]
    cfg.crypto_x_accounts = [a.lstrip("@") for a in cfg.crypto_x_accounts]
    cfg.notify_service = cfg.notify_service.strip().removeprefix("notify.")
    if cfg.summary_interval_minutes < 15:
        cfg.summary_interval_minutes = 15
    if cfg.anthropic_effort not in ("low", "medium", "high", "xhigh", "max"):
        cfg.anthropic_effort = "medium"
    if cfg.alert_sensitivity not in SENSITIVITY:
        cfg.alert_sensitivity = "normaal"
    return cfg
