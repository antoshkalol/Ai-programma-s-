"""Knoopt alle onderdelen aan elkaar en plant de jobs in (per markt: aandelen en crypto)."""

from __future__ import annotations

import logging

from .alerts import AlertEngine
from .config import Config
from .db import Database, ago, iso, utcnow
from .filtering import NewsIngestor
from .hass import HomeAssistant
from .net import make_session
from .scheduler import Job, Scheduler
from .scoring import CryptoScoringEngine, ScoringEngine
from .sources.btc_whales import BtcWhaleSource
from .sources.coingecko import CoinGeckoHistory, CoinGeckoMarkets
from .sources.house import HouseSource
from .sources.news import GdeltSource, GoogleNewsSource, RssSource
from .sources.prices import PriceSource
from .sources.quiver import QuiverSource
from .sources.sec_form4 import SecForm4Source
from .sources.sec_treasury import SecTreasurySource
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
        self.extractors = {
            "stocks": TickerExtractor(self.db, cfg.stock_watchlist, "stocks"),
            "crypto": TickerExtractor(self.db, cfg.crypto_watchlist, "crypto"),
        }
        self.ingestor = NewsIngestor(cfg, self.db, self.extractors)
        self.scoring = {"stocks": ScoringEngine(cfg, self.db), "crypto": CryptoScoringEngine(cfg, self.db)}
        self.summarizer = Summarizer(cfg, self.db, self.ingestor)
        self.hass = HomeAssistant(cfg, self.db)
        self.alerts = AlertEngine(cfg, self.db, self.hass, self.summarizer)
        self.scheduler = Scheduler(self.db, workers=4)
        self.job_markets: dict[str, str] = {}
        self._register_jobs()

    # ------------------------------------------------------------------ jobs
    def _add(self, market: str, job: Job) -> None:
        self.scheduler.add(job)
        self.job_markets[job.name] = market

    def _news_jobs(self, market: str) -> None:
        cfg, s = self.cfg, self.session
        ingest = self.ingestor.for_market(market)
        if cfg.enable_rss:
            self._add(market, Job(f"rss_{market}", RssSource(cfg, s, ingest, market).run, 5 * MIN, initial_delay=2))
        if cfg.enable_google_news:
            self._add(market, Job(f"google_news_{market}", GoogleNewsSource(cfg, s, ingest, market).run, 10 * MIN,
                                  initial_delay=10))
        if cfg.enable_gdelt:
            self._add(market, Job(f"gdelt_{market}", GdeltSource(cfg, s, ingest, market).run, 15 * MIN,
                                  initial_delay=15 if market == "stocks" else 25))
        if cfg.x_bearer_token and cfg.x_accounts(market):
            self._add(market, Job(f"x_{market}", XSource(cfg, self.db, s, ingest, market).run, 15 * MIN,
                                  initial_delay=30))

    def _register_jobs(self) -> None:
        cfg, db, s = self.cfg, self.db, self.session
        if cfg.enable_stocks:
            if cfg.enable_sec_form4:
                self._add("stocks", Job("sec_form4", self._trade_job(SecForm4Source(cfg, db, make_session())),
                                        5 * MIN, initial_delay=5))
            if cfg.enable_house:
                self._add("stocks", Job("house", self._trade_job(HouseSource(cfg, db, make_session())),
                                        6 * HOUR, initial_delay=40))
            if cfg.enable_senate:
                self._add("stocks", Job("senate", self._trade_job(SenateSource(cfg, db, make_session())),
                                        6 * HOUR, initial_delay=70))
            if cfg.quiver_api_key:
                self._add("stocks", Job("quiver", self._trade_job(QuiverSource(cfg, db, s)), 1 * HOUR,
                                        initial_delay=20))
            self._news_jobs("stocks")
            prices = PriceSource(cfg, db, make_session(), self.scoring["stocks"].candidate_tickers)
            self._add("stocks", Job("prices_stocks", prices.run, 15 * MIN, initial_delay=90))
            self._add("stocks", Job("scoring_stocks", self._scoring_job("stocks"), 15 * MIN, initial_delay=150))
        if cfg.enable_crypto:
            cg = make_session()
            self._add("crypto", Job("crypto_markets", CoinGeckoMarkets(cfg, db, cg).run, 3 * MIN, initial_delay=3))
            history = CoinGeckoHistory(cfg, db, cg, self.scoring["crypto"].candidate_tickers)
            self._add("crypto", Job("crypto_history", history.run, 3 * HOUR, initial_delay=45))
            if cfg.enable_crypto_treasury:
                self._add("crypto", Job("crypto_treasury", self._trade_job(SecTreasurySource(cfg, db, make_session())),
                                        30 * MIN, initial_delay=35))
            if cfg.enable_btc_whales:
                self._add("crypto", Job("btc_whales", self._trade_job(BtcWhaleSource(cfg, db, make_session())),
                                        5 * MIN, initial_delay=60))
            self._news_jobs("crypto")
            self._add("crypto", Job("scoring_crypto", self._scoring_job("crypto"), 15 * MIN, initial_delay=120))
        self._add("all", Job("summary", self._summary_job, cfg.summary_interval_minutes * MIN,
                             initial_delay=5 * MIN, aligned=True))
        if cfg.alerts_enabled:
            self._add("all", Job("alerts", self.alerts.run, 2 * MIN, initial_delay=4 * MIN))
        self._add("all", Job("calibration", self._calibrate, 24 * HOUR, initial_delay=10 * MIN))
        self._add("all", Job("cleanup", self.cleanup, 24 * HOUR, initial_delay=1 * HOUR))

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

    def _scoring_job(self, market: str):
        def run() -> int:
            n = self.scoring[market].run()
            self.hass.publish_scores(market)
            jobs = self.db.query("SELECT name, last_ok, last_error FROM job_status")
            failing = [j["name"] for j in jobs if j["last_error"]]
            self.hass.publish_status(not failing, {"bronnen_met_fouten": failing, "bijgewerkt": iso(utcnow())})
            return n

        return run

    def _summary_job(self) -> int:
        n = self.summarizer.run()
        self.hass.publish_summaries(self.cfg.markets)
        return n

    def _calibrate(self) -> int:
        return sum(self.scoring[m].calibrate() for m in self.cfg.markets)

    def cleanup(self) -> int:
        stmts = [
            ("DELETE FROM news WHERE published_at < ?", ago(days=14)),
            ("DELETE FROM trades WHERE created_at < ?", ago(days=365)),
            ("DELETE FROM ticker_sentiment WHERE created_at < ?", ago(days=7)),
            ("DELETE FROM summaries WHERE created_at < ?", ago(days=30)),
            ("DELETE FROM alerts WHERE created_at < ?", ago(days=90)),
            ("DELETE FROM seen WHERE created_at < ?", ago(days=120)),
            ("DELETE FROM prices WHERE date < ?", ago(days=400)[:10]),
            ("DELETE FROM score_history WHERE day < ?", ago(days=400)[:10]),
            ("DELETE FROM crypto_snapshots WHERE ts < ?", ago(days=3)),
        ]
        total = 0
        for sql, arg in stmts:
            total += max(0, self.db.execute(sql, (arg,)))
        return total

    def start(self) -> None:
        self.scheduler.start()
        log.info("Planner gestart met jobs: %s", ", ".join(self.scheduler.jobs))
