"""In-process background workers: process webhook_events and deliver the outbox.

Plain threads over PostgreSQL (FOR UPDATE SKIP LOCKED), so several threads — or several app instances —
can run side by side without Redis or a broker. The webhook wakes the workers right after ingesting; the
poll interval is only a safety net for retries, stale leases and anything left behind by a crash."""
import threading
import time

from app.core.config import settings
from app.core.logging import get_logger, log_event
from app.db.session import SessionLocal
from app.ops import purge_processed_events
from app.services.messaging_service import deliver_due, recover_stale_sends
from app.workflows.inbound import run_due

logger = get_logger(__name__)


class BackgroundWorkers:
    def __init__(self, session_factory=SessionLocal):
        self.session_factory = session_factory
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._last_recovery = 0.0
        self._last_purge = 0.0

    def start(self, count: int) -> None:
        if self._threads or count <= 0:
            return
        self._stop.clear()
        for i in range(count):
            t = threading.Thread(target=self._loop, name=f"duka-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        log_event(logger, "workers.started", operation="workers", count=count)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        for t in self._threads:
            t.join(timeout)
        self._threads = []

    def wake(self) -> None:
        self._wake.set()

    @property
    def running(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    def run_once(self) -> int:
        """One pass: process due events, deliver due outbox messages, recover stale sends. Returns work done."""
        done = len(run_due(self.session_factory, limit=20))
        done += deliver_due(self.session_factory)
        if time.monotonic() - self._last_recovery > 30:
            self._last_recovery = time.monotonic()
            done += recover_stale_sends(self.session_factory)
        if time.monotonic() - self._last_purge > 3600:
            self._last_purge = time.monotonic()
            with self.session_factory() as db:
                purged = purge_processed_events(db, settings.webhook_event_retention_days)
                db.commit()
            if purged:
                log_event(logger, "webhook_events.purged", operation="retention", count=purged)
        return done

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                busy = self.run_once() > 0
            except Exception as exc:  # e.g. database briefly unavailable: keep the worker alive
                log_event(logger, "workers.error", 40, operation="workers", status="error", error=repr(exc)[:300])
                busy = False
            if not busy:
                self._wake.wait(settings.worker_poll_seconds)
                self._wake.clear()


workers = BackgroundWorkers()
