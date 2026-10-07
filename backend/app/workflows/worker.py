"""In-process background workers: process webhook_events and deliver the outbox.

Plain threads over PostgreSQL (FOR UPDATE SKIP LOCKED), so several threads — or several app instances —
can run side by side without Redis or a broker. The webhook wakes the workers right after ingesting; the
poll interval is only a safety net for retries, stale leases and anything left behind by a crash."""
import threading
import time
import uuid

from app.core.config import settings
from app.core.logging import get_logger, log_event, safe_error
from app.db.session import SessionLocal
from app.ops import purge_processed_events
from app.services.messaging_service import deliver_due, recover_stale_sends
from app.workflows.inbound import claim, process_event, release

logger = get_logger(__name__)


class BackgroundWorkers:
    def __init__(self, session_factory=SessionLocal):
        self.session_factory = session_factory
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._last_recovery = 0.0
        self._last_purge = 0.0
        self._inflight: set[uuid.UUID] = set()  # events claimed by this process and not finished yet
        self._inflight_lock = threading.Lock()

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
        """Stop claiming, give running work `timeout` seconds in total, then hand back whatever is unfinished so
        it is processed right away (by the next instance) instead of after its lease."""
        self._stop.set()
        self._wake.set()
        deadline = time.monotonic() + timeout
        for t in self._threads:
            t.join(max(0.0, deadline - time.monotonic()))
        self._threads = []
        self._release_unfinished()

    def _release_unfinished(self) -> None:
        with self._inflight_lock:
            unfinished = list(self._inflight)
        if not unfinished:
            return
        try:
            released = release(unfinished, self.session_factory)
        except Exception as exc:  # database unreachable too: the leases expire on their own
            log_event(logger, "workers.release_failed", 40, operation="workers", status="error",
                      error=safe_error(exc, 300))
            return
        log_event(logger, "workers.leases_released", 30, operation="workers", count=released)

    def wake(self) -> None:
        self._wake.set()

    @property
    def running(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    def _process_inbound(self, limit: int) -> int:
        """inbound.run_due for the workers: stops claiming once shutdown begins and tracks what is in flight."""
        done = 0
        while done < limit and not self._stop.is_set():
            claimed = claim(self.session_factory)
            if claimed is None:
                break
            with self._inflight_lock:
                self._inflight.add(claimed[0])
            try:
                process_event(*claimed, session_factory=self.session_factory)
            finally:
                with self._inflight_lock:
                    self._inflight.discard(claimed[0])
            done += 1
        return done

    def run_once(self) -> int:
        """One pass: process due events, deliver due outbox messages, recover stale sends. Returns work done."""
        done = self._process_inbound(limit=20)
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
