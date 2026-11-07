"""Startpunt: python -m market_monitor --data-dir /data --port 8099"""

from __future__ import annotations

import argparse
import logging
import sys

from waitress import serve

from . import __version__
from .app import MarketMonitor
from .config import load_config
from .web import create_app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Market Monitor")
    parser.add_argument("--data-dir", default="/data")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--no-scheduler", action="store_true", help="alleen dashboard, geen data ophalen")
    args = parser.parse_args(argv)

    cfg = load_config(args.data_dir, args.port)
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("waitress").setLevel(logging.WARNING)
    log = logging.getLogger("market_monitor")
    log.info("Market Monitor %s start (data: %s, poort: %s)", __version__, cfg.data_dir, cfg.port)
    if not cfg.sec_headers_ok:
        log.warning("Tip: vul bij de opties 'sec_user_agent' in als 'Naam jouw@email.nl' (verplicht door de SEC).")
    if cfg.llm_enabled:
        log.info("AI-samenvattingen actief met model %s", cfg.anthropic_model)
    else:
        log.info("Geen Anthropic API-sleutel: automatische samenvattingen zonder AI.")

    monitor = MarketMonitor(cfg)
    if not args.no_scheduler:
        monitor.start()
    serve(create_app(monitor), host=args.host, port=cfg.port, threads=6, ident="MarketMonitor")


if __name__ == "__main__":
    main()
