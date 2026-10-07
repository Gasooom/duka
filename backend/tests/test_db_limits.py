"""Database safety limits and worker leases. Every app connection carries statement, lock and idle-in-transaction
timeouts sized around the AI turn budget; a lock wait that runs out fails the event into a retry (never a lost
message); a transaction left idle is ended and the pool recovers; a graceful shutdown hands unfinished events back
at once instead of after the lease."""
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError as SettingsError
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings, settings
from app.db.session import SessionLocal
from app.models import Conversation, WebhookEvent
from app.workflows import worker as worker_module
from app.workflows.inbound import ProcessResult, claim, process_event, release
from app.workflows.worker import BackgroundWorkers
from tests.conftest import drain

NUMBER = "250788111222"


def _event(external_id: str | None = None) -> WebhookEvent:
    with SessionLocal() as s:
        stmt = select(WebhookEvent).order_by(WebhookEvent.seq.desc())
        return s.scalars(stmt.where(WebhookEvent.external_id == external_id) if external_id else stmt).first()


def test_every_app_connection_carries_the_limits():
    with SessionLocal() as s:
        shown = [s.execute(text(f"SHOW {name}")).scalar() for name in
                 ("statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout")]
    assert shown == ["1min", "50s", "2min"]


def test_default_limits_fit_around_the_ai_turn_budget():
    turn_ms = settings.agent_turn_timeout_seconds * 1000
    assert settings.db_lock_timeout_ms > turn_ms  # a waiter outlasts a full AI turn holding the lock
    assert settings.db_statement_timeout_ms >= settings.db_lock_timeout_ms
    assert settings.db_idle_in_transaction_timeout_ms > turn_ms  # the open transaction during LLM calls survives
    assert settings.webhook_lease_seconds == 120  # down from 300
    assert settings.webhook_lease_seconds * 1000 > settings.db_lock_timeout_ms + turn_ms


@pytest.mark.parametrize("bad", [
    {"db_lock_timeout_ms": 30_000},                 # shorter than a 45 s AI turn
    {"db_statement_timeout_ms": 40_000},            # would cut lock waits short
    {"db_idle_in_transaction_timeout_ms": 20_000},  # would kill the transaction around an LLM call
    {"webhook_lease_seconds": 60},                  # would expire while the event is still being processed
    {"agent_turn_timeout_seconds": 90},             # a longer turn needs longer limits too
])
def test_inconsistent_limits_are_refused_at_startup(bad):
    with pytest.raises(SettingsError):
        Settings(**bad)


def test_limits_can_be_switched_off():
    Settings(db_statement_timeout_ms=0, db_lock_timeout_ms=0, db_idle_in_transaction_timeout_ms=0)


def test_a_lock_wait_that_runs_out_fails_into_a_retry_not_a_lost_message(fashion, outbox):
    fashion.send("hello", from_number=NUMBER)  # the customer and conversation exist
    fashion.send("black sneakers", from_number=NUMBER, wa_id="wamid.LOCKED", process=False)
    blocker = SessionLocal()
    blocker.execute(select(Conversation.id).with_for_update())  # e.g. a long AI turn on the same conversation

    def short_lock_wait():  # the production limit is 50 s; same mechanism, shorter wait
        s = SessionLocal()
        s.execute(text("SET LOCAL lock_timeout = '300ms'"))
        return s

    try:
        claimed = claim(SessionLocal)
        result = process_event(*claimed, session_factory=short_lock_wait)
    finally:
        blocker.rollback()
        blocker.close()
    event = _event("wamid.LOCKED")
    assert result.status == "error" and event.status == "retry" and "LockNotAvailable" in event.last_error
    with SessionLocal() as s:
        s.execute(update(WebhookEvent).values(next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        s.commit()
    assert [r.status for r in drain()] == ["replied"]
    assert _event("wamid.LOCKED").status == "done" and len(outbox.sent) == 2  # one reply per message, none lost


def test_a_transaction_left_idle_is_ended_and_the_pool_recovers():
    s = SessionLocal()
    s.execute(text("SET LOCAL idle_in_transaction_session_timeout = '300ms'"))
    time.sleep(1.0)  # e.g. a request stuck between statements while holding a transaction open
    with pytest.raises(DBAPIError, match="idle-in-transaction timeout"):
        s.execute(text("SELECT 1"))
    s.close()
    with SessionLocal() as fresh:
        assert fresh.execute(text("SELECT 1")).scalar() == 1


def test_graceful_shutdown_hands_unfinished_events_back_at_once(fashion, monkeypatch):
    started, finish = threading.Event(), threading.Event()

    def slow_turn(event_id, attempts, session_factory=SessionLocal):  # an AI turn outliving the shutdown grace
        started.set()
        finish.wait(15)
        return ProcessResult(status="error")

    monkeypatch.setattr(worker_module, "process_event", slow_turn)
    fashion.send("black sneakers", from_number=NUMBER, wa_id="wamid.SHUTDOWN", process=False)
    workers = BackgroundWorkers()
    workers.start(1)
    threads = list(workers._threads)
    try:
        assert started.wait(15)
        assert _event("wamid.SHUTDOWN").status == "processing"
        workers.stop(timeout=0.2)
        event = _event("wamid.SHUTDOWN")
        assert (event.status, event.locked_until, event.attempts) == ("retry", None, 0)  # attempt not counted
        assert claim(SessionLocal) is not None  # claimable now, not after the 120 s lease
    finally:
        finish.set()
        for t in threads:
            t.join(15)


def test_release_leaves_finished_and_unknown_events_alone(fashion, outbox):
    fashion.send("black sneakers", from_number=NUMBER, wa_id="wamid.DONE")
    done = _event("wamid.DONE")
    assert done.status == "done"
    assert release([done.id]) == 0 and _event("wamid.DONE").status == "done"
    assert release([]) == 0


def test_shutdown_without_work_in_flight_releases_nothing(fashion, outbox):
    workers = BackgroundWorkers()
    workers.start(1)
    fashion.send("black sneakers", from_number=NUMBER, process=False)
    workers.wake()
    deadline = time.monotonic() + 15
    while not outbox.sent and time.monotonic() < deadline:
        time.sleep(0.1)
    workers.stop()
    assert _event().status == "done" and len(outbox.sent) == 1
