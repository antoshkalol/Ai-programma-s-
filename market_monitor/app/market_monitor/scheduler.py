"""Eenvoudige job-planner: elke job heeft een interval, jobs overlappen zichzelf nooit."""

from __future__ import annotations

import logging
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from .db import Database

log = logging.getLogger(__name__)


@dataclass
class Job:
    name: str
    func: Callable[[], int | None]
    interval: float                 # seconden
    initial_delay: float = 0.0
    aligned: bool = False           # True: draait op veelvouden van het interval (bv. :00 en :30)
    next_run: float = field(default=0.0)
    running: bool = field(default=False)


class Scheduler:
    def __init__(self, db: Database, workers: int = 3):
        self.db = db
        self.jobs: dict[str, Job] = {}
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="job")
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def add(self, job: Job) -> None:
        now = time.time()
        if job.aligned:
            job.next_run = self._next_aligned(now, job.interval)
            if job.initial_delay:
                job.next_run = min(job.next_run, now + job.initial_delay)
        else:
            job.next_run = now + job.initial_delay
        self.jobs[job.name] = job

    @staticmethod
    def _next_aligned(now: float, interval: float) -> float:
        return (int(now // interval) + 1) * interval

    def trigger(self, name: str) -> bool:
        job = self.jobs.get(name)
        if not job:
            return False
        with self._lock:
            job.next_run = 0
        return True

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            for job in list(self.jobs.values()):
                with self._lock:
                    due = not job.running and now >= job.next_run
                    if due:
                        job.running = True
                if due:
                    self._pool.submit(self._run, job)
            self._stop.wait(1.0)

    def run_now(self, name: str) -> int | None:
        """Draait een job synchroon (voor tests en de eerste start)."""
        job = self.jobs[name]
        with self._lock:
            if job.running:
                return None
            job.running = True
        return self._run(job)

    def _run(self, job: Job) -> int | None:
        started = time.time()
        self.db.job_started(job.name)
        result = None
        try:
            result = job.func()
            self.db.job_finished(job.name, items=int(result or 0))
            log.info("Job %s klaar in %.1fs (%s items)", job.name, time.time() - started, result or 0)
        except Exception as exc:  # noqa: BLE001 - een job mag de planner nooit stoppen
            log.error("Job %s mislukt: %s\n%s", job.name, exc, traceback.format_exc())
            self.db.job_finished(job.name, error=f"{type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                now = time.time()
                if job.aligned:
                    job.next_run = self._next_aligned(now, job.interval)
                else:
                    job.next_run = now + job.interval
                job.running = False
        return result
