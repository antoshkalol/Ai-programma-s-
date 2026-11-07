"""Knoopt alle onderdelen aan elkaar en plant de jobs in."""

from __future__ import annotations

import logging

from .config import Config
from .db import Database, ago, iso, utcnow
from .filtering import NewsIngestor
from .hass import HomeAssistant
from .net import make_session
from .scheduler import Job, Scheduler
from .scoring import ScoringEngine
from .sources.house import HouseSource
from .sources.news import GdeltSource, GoogleNewsSource, RssSource
from .sources.prices import PriceSource
from .sources.quiver import QuiverSource
from .sources.sec_form4 import SecForm4Source
from .sources.senate import SenateSource
from .sources.x_posts import XSource
from .summary import Summarizer
from .tickers import TickerExtractor

log = logging.getLogger(__name__)

MIN = 60
HOUR = 3600


class MarketMonitor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.session = make_session()
        self.extractor = TickerExtractor(self.db, cfg.watchlist)
        self.ingestor = NewsIngestor(cfg, self.db, self.extractor)
        self.scoring = ScoringEngine(cfg, self.db)
        self.summarizer = Summarizer(cfg, self.db, self.ingestor)
        self.hass = HomeAssistant(cfg, self.db)
        self.scheduler = Scheduler(self.db, workers=3)
        self._register_jobs()

    # ------------------------------------------------------------------ jobs
    def _register_jobs(self) -> None:
        cfg, db, s = self.cfg, self.db, self.session
        add = self.scheduler.add
        if cfg.enable_sec_form4:
            src = SecForm4Source(cfg, db, make_session())
            add(Job("sec_form4", self._trade_job(src), 5 * MIN, initial_delay=5))
        if cfg.enable_house:
            src = HouseSource(cfg, db, make_session())
            add(Job("house", self._trade_job(src), 6 * HOUR, initial_delay=30))
        if cfg.enable_senate:
            src = SenateSource(cfg, db, make_session())
            add(Job("senate", self._trade_job(src), 6 * HOUR, initial_delay=60))
        if cfg.quiver_api_key:
            src = QuiverSource(cfg, db, s)
            add(Job("quiver", self._trade_job(src), 1 * HOUR, initial_delay=20))
        if cfg.enable_rss:
            add(Job("rss", RssSource(cfg, s, self.ingestor).run, 5 * MIN, initial_delay=2))
        if cfg.enable_google_news:
            add(Job("google_news", GoogleNewsSource(cfg, s, self.ingestor).run, 10 * MIN, initial_delay=10))
        if cfg.enable_gdelt:
            add(Job("gdelt", GdeltSource(cfg, s, self.ingestor).run, 15 * MIN, initial_delay=15))
        if cfg.x_bearer_token:
            add(Job("x_posts", XSource(cfg, db, s, self.ingestor).run, 15 * MIN, initial_delay=25))
        prices = PriceSource(cfg, db, make_session(), self.scoring.candidate_tickers)
        add(Job("prices", prices.run, 30 * MIN, initial_delay=90))
        add(Job("scoring", self._scoring_job, 15 * MIN, initial_delay=150))
        add(Job("summary", self._summary_job, cfg.summary_interval_minutes * MIN, initial_delay=4 * MIN,
                aligned=True))
        add(Job("calibration", self.scoring.calibrate, 24 * HOUR, initial_delay=10 * MIN))
        add(Job("cleanup", self.cleanup, 24 * HOUR, initial_delay=1 * HOUR))

    def _trade_job(self, source):
        def run() -> int:
            n = source.run()
            self._notify_new_trades()
            return n

        return run

    def _notify_new_trades(self) -> None:
        last = self.db.get_state("ha_last_trade_ts") or ago(minutes=30)
        rows = self.db.query("SELECT * FROM trades WHERE created_at > ? ORDER BY created_at", (last,))
        if rows:
            self.hass.publish_new_trades(rows)
            self.db.set_state("ha_last_trade_ts", rows[-1]["created_at"])

    def _scoring_job(self) -> int:
        n = self.scoring.run()
        self.hass.publish_scores()
        jobs = self.db.query("SELECT name, last_ok, last_error FROM job_status")
        failing = [j["name"] for j in jobs if j["last_error"]]
        self.hass.publish_status(not failing, {"bronnen_met_fouten": failing, "bijgewerkt": iso(utcnow())})
        return n

    def _summary_job(self) -> int:
        n = self.summarizer.run()
        self.hass.publish_summary()
        return n

    def cleanup(self) -> int:
        stmts = [
            ("DELETE FROM news WHERE published_at < ?", ago(days=14)),
            ("DELETE FROM trades WHERE created_at < ?", ago(days=365)),
            ("DELETE FROM ticker_sentiment WHERE created_at < ?", ago(days=7)),
            ("DELETE FROM summaries WHERE created_at < ?", ago(days=30)),
            ("DELETE FROM seen WHERE created_at < ?", ago(days=120)),
            ("DELETE FROM prices WHERE date < ?", ago(days=400)[:10]),
            ("DELETE FROM score_history WHERE day < ?", ago(days=400)[:10]),
        ]
        total = 0
        for sql, arg in stmts:
            total += max(0, self.db.execute(sql, (arg,)))
        return total

    def start(self) -> None:
        self.scheduler.start()
        log.info("Planner gestart met jobs: %s", ", ".join(self.scheduler.jobs))
