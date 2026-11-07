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

# Modellen die de server-side fallback ("fallbacks": "default") ondersteunen.
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


@dataclass
class Config:
    # Verplicht door de SEC: "Naam e-mailadres" (wordt alleen naar sec.gov gestuurd).
    sec_user_agent: str = ""

    # AI-samenvatting (optioneel). Zonder sleutel maakt de app een automatische samenvatting.
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: str = "medium"
    summary_language: str = "nl"
    summary_interval_minutes: int = 30

    # Optionele betaalde/extra bronnen
    quiver_api_key: str = ""
    x_bearer_token: str = ""
    x_accounts: list[str] = field(
        default_factory=lambda: [
            "elonmusk",
            "realDonaldTrump",
            "WhiteHouse",
            "federalreserve",
            "SECGov",
            "Reuters",
            "business",
            "WSJmarkets",
            "CNBC",
            "DeItaone",
        ]
    )

    # Bronnen aan/uit
    enable_sec_form4: bool = True
    enable_house: bool = True
    enable_senate: bool = True
    enable_rss: bool = True
    enable_google_news: bool = True
    enable_gdelt: bool = True

    news_queries: list[str] = field(
        default_factory=lambda: [
            "stock market",
            "earnings",
            "insider buying",
            "Federal Reserve",
            "Wall Street",
        ]
    )
    extra_rss_feeds: list[str] = field(default_factory=list)
    trusted_domains: list[str] = field(default_factory=list)
    blocked_domains: list[str] = field(default_factory=list)

    watchlist: list[str] = field(
        default_factory=lambda: [
            "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "JPM", "ASML",
        ]
    )

    # Drempels
    min_trade_value_usd: int = 100_000
    big_trade_alert_usd: int = 1_000_000
    min_news_credibility: float = 0.45

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

    def public_dict(self) -> dict:
        """Configuratie zonder geheimen, voor het dashboard."""
        d = asdict(self)
        for secret in ("anthropic_api_key", "quiver_api_key", "x_bearer_token"):
            d[secret] = bool(d[secret])
        d["sec_user_agent"] = bool(self.sec_headers_ok)
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

    for f in fields(Config):
        if f.name in ("data_dir", "port"):
            continue
        if f.name in raw:
            setattr(cfg, f.name, _coerce(raw[f.name], getattr(cfg, f.name)))

    # Omgevingsvariabelen gaan voor (handig bij lokaal testen)
    env_map = {
        "ANTHROPIC_API_KEY": "anthropic_api_key",
        "MM_SEC_USER_AGENT": "sec_user_agent",
        "MM_QUIVER_API_KEY": "quiver_api_key",
        "MM_X_BEARER_TOKEN": "x_bearer_token",
    }
    for env, attr in env_map.items():
        if os.environ.get(env) and not getattr(cfg, attr):
            setattr(cfg, attr, os.environ[env])

    cfg.watchlist = [t.upper() for t in cfg.watchlist]
    cfg.x_accounts = [a.lstrip("@") for a in cfg.x_accounts]
    if cfg.summary_interval_minutes < 5:
        cfg.summary_interval_minutes = 5
    if cfg.anthropic_effort not in ("low", "medium", "high", "xhigh", "max"):
        cfg.anthropic_effort = "medium"
    return cfg
