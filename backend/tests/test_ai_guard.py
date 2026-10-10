"""Runaway Conversation Guard, B2: counters and observe mode (docs/P1_RUNAWAY_GUARD.md, app/services/ai_guard.py).

Every real model call and every provider HTTP attempt is reserved in ai_usage_counters before it is made, per inbound
message (the durable webhook event, across retries), per customer and per tenant in fixed UTC hour/day buckets.
Observe mode counts and logs what a limit would have refused, and never changes a reply or blocks a call."""
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.agents.providers import LLMProvider, LLMResponse, ToolCall, set_provider_override
from app.core.config import Settings, settings
from app.db.session import SessionLocal, engine
from app.models import AgentRun, AiUsageCounter, UsageEvent, WebhookEvent
from app.ops import metrics, purge_ai_usage_counters
from app.repositories.repos import UsageEventRepo
from app.services import ai_guard
from app.services.ai_guard import AIGuard
from app.workflows import inbound
from tests.conftest import drain
from tests.test_usage_metering import completion, openai_provider

NUMBER = "250788111222"


class Looping(LLMProvider):
    """A metered model that never stops asking for tools: the runaway case."""
    name = "looping"

    def __init__(self):
        self.calls = 0
        self._lock = threading.Lock()

    def complete(self, messages, tools, *, model=None, temperature=0.2, timeout=None):
        with self._lock:
            self.calls += 1
        if not tools:
            return LLMResponse(content="summary")
        return LLMResponse(content=None, tool_calls=[ToolCall("c1", "search_products", {"query": "black sneakers"})])


def counters(**where) -> list[AiUsageCounter]:
    with SessionLocal() as s:
        return list(s.scalars(select(AiUsageCounter).filter_by(**where)
                              .order_by(AiUsageCounter.scope, AiUsageCounter.period)))


def by_scope(rows: list[AiUsageCounter]) -> dict[tuple[str, str], tuple[int, int]]:
    return {(r.scope, r.period): (r.calls, r.attempts) for r in rows}


@pytest.fixture(autouse=True)
def observe_everything(monkeypatch):
    """These are the observe-mode tests (B2): every scope observes, whatever the deployment default (the message
    budget enforces by default since B3, tested in test_ai_guard_enforce.py)."""
    for scope in ("message", "customer", "tenant"):
        monkeypatch.setattr(settings, f"ai_guard_{scope}_mode", "observe")


def modes(monkeypatch, mode: str) -> None:
    for scope in ("message", "customer", "tenant"):
        monkeypatch.setattr(settings, f"ai_guard_{scope}_mode", mode)


def tiny_limits(monkeypatch) -> None:
    """Limits far below what one runaway turn uses: in observe mode they must change nothing."""
    monkeypatch.setattr(settings, "ai_guard_message_calls", 1)
    for scope in ("customer", "tenant"):
        for unit in ("calls", "attempts"):
            for period in ("hour", "day"):
                monkeypatch.setattr(settings, f"ai_guard_{scope}_{unit}_per_{period}", 1)


def replies_to(outbox, number: str) -> list[str]:
    return [b for to, b in outbox.sent if to == number]


# ---------------------------------------------------------------- observe mode changes nothing
def test_observe_mode_counts_every_call_and_changes_no_reply(fashion, outbox, monkeypatch, caplog):
    tiny_limits(monkeypatch)
    model = Looping()
    set_provider_override(model)
    modes(monkeypatch, "off")
    fashion.send("black sneakers please", from_number="250788000001")
    calls_off = model.calls
    assert counters() == []  # off: nothing counted
    modes(monkeypatch, "observe")
    with caplog.at_level(logging.WARNING, logger="app"):
        fashion.send("black sneakers please", from_number="250788000002")
    assert model.calls - calls_off == calls_off == 5  # the same five calls: nothing was blocked
    assert replies_to(outbox, "250788000002") == replies_to(outbox, "250788000001")  # the same reply
    rows = counters()
    assert by_scope(rows) == {("customer", "day"): (5, 5), ("customer", "hour"): (5, 5),
                              ("message", "lifetime"): (5, 5), ("tenant", "day"): (5, 5), ("tenant", "hour"): (5, 5)}
    assert {r.over_limit for r in rows} == {4} and {r.denied for r in rows} == {0}  # calls 2..5 were past limit 1
    decisions = [r for r in caplog.records if r.getMessage() == "ai_guard.decision"]
    assert decisions and {r.extra_fields["decision"] for r in decisions} == {"would_block"}
    assert {r.extra_fields["mode"] for r in decisions} == {"observe"}


def test_the_default_message_budget_is_one_processing_attempt(monkeypatch):
    assert settings.ai_guard_message_limits == (1 + settings.agent_max_tool_iterations,
                                                (1 + settings.agent_max_tool_iterations) * settings.llm_max_attempts)
    monkeypatch.setattr(settings, "ai_guard_message_calls", 4)
    assert settings.ai_guard_message_limits == (4, 4 * settings.llm_max_attempts)


# ---------------------------------------------------------------- the message budget follows the webhook event
def _crash_after_the_model(monkeypatch):
    def crash(*a, **k):
        raise RuntimeError("database error after the model calls")
    monkeypatch.setattr(inbound, "send_to_customer", crash)


def _make_retries_due() -> None:
    with SessionLocal() as s:
        s.execute(update(WebhookEvent).where(WebhookEvent.status == "retry")
                  .values(next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        s.commit()


def test_a_retried_message_is_counted_against_one_budget_across_retries(fashion, outbox, monkeypatch):
    set_provider_override(Looping())
    _crash_after_the_model(monkeypatch)
    fashion.send("black sneakers please", wa_id="wamid.RETRY")
    for _ in range(settings.webhook_max_attempts + 1):
        _make_retries_due()
        drain()
    with SessionLocal() as s:
        event = s.scalars(select(WebhookEvent)).one()
    assert (event.status, event.attempts) == ("dead", settings.webhook_max_attempts)
    [message_row] = counters(scope="message")
    assert message_row.subject_id == event.id  # the durable event, the same row on every retry
    assert (message_row.calls, message_row.attempts) == (25, 25)  # what B3 will bound: 5 attempts x 5 calls


def test_reservations_survive_the_rollback_of_the_turn(fashion, outbox, monkeypatch):
    set_provider_override(Looping())
    _crash_after_the_model(monkeypatch)
    fashion.send("black sneakers please")
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(AgentRun)) == 0  # the turn rolled back
    assert by_scope(counters(scope="tenant"))[("tenant", "hour")] == (5, 5)  # its reservations did not


# ---------------------------------------------------------------- provider attempts
def test_every_provider_http_attempt_is_reserved(fashion, outbox, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    replies = [httpx.Response(503, text="busy"), httpx.Response(429, text="slow down"),
               completion("Which size do you wear?", usage=(80, 9))]
    set_provider_override(openai_provider(lambda req: replies.pop(0)))
    fashion.send("black sneakers please")
    assert set(by_scope(counters()).values()) == {(1, 3)}  # one call, three HTTP attempts, in every scope
    with SessionLocal() as s:
        assert s.scalars(select(UsageEvent.attempts).where(UsageEvent.kind == "llm_call")).one() == 3  # the ledger agrees


def test_an_unknown_outcome_is_counted_before_it_is_known(fashion, monkeypatch):
    """A timeout after the request was sent may still be billed: the attempt was reserved before sending it."""
    monkeypatch.setattr("time.sleep", lambda s: None)

    def timeout(req):
        raise httpx.ReadTimeout("no answer", request=req)

    set_provider_override(openai_provider(timeout, max_attempts=2))
    fashion.send("black sneakers please")
    assert by_scope(counters(scope="tenant"))[("tenant", "hour")] == (1, 2)


# ---------------------------------------------------------------- correctness under concurrency, isolation
def test_concurrent_reservations_lose_no_update(fashion):
    bid = uuid.UUID(fashion.business_id)
    workers, each = 8, 5
    barrier = threading.Barrier(workers)

    def work():
        guard = AIGuard(engine, bid, customer_id=uuid.uuid4())
        barrier.wait()
        for _ in range(each):
            guard.reserve_call()

    threads = [threading.Thread(target=work) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    tenant = by_scope(counters(scope="tenant"))
    assert tenant[("tenant", "hour")] == tenant[("tenant", "day")] == (workers * each, workers * each)
    assert len(counters(scope="customer")) == workers * 2


def test_each_tenant_is_counted_apart_and_cannot_move_its_counters(fashion, electronics, outbox, db):
    set_provider_override(Looping())
    fashion.send("black sneakers please")
    electronics.send("samsung phone")
    a, b = uuid.UUID(fashion.business_id), uuid.UUID(electronics.business_id)
    assert {(r.business_id, r.subject_id) for r in counters(scope="tenant")} == {(a, a), (b, b)}
    assert by_scope(counters(scope="tenant", business_id=a))[("tenant", "hour")] == (5, 5)
    with pytest.raises(IntegrityError, match="immutable"):
        db.execute(text("UPDATE ai_usage_counters SET business_id = :b WHERE business_id = :a"), {"a": a, "b": b})
    db.rollback()


# ---------------------------------------------------------------- failures
def test_a_counter_store_failure_never_blocks_in_observe_mode(fashion, outbox, monkeypatch, caplog):
    def broken(*a, **k):
        raise RuntimeError("counter store unavailable")

    monkeypatch.setattr(ai_guard, "_upsert", broken)
    model = Looping()
    set_provider_override(model)
    with caplog.at_level(logging.ERROR, logger="app"):
        fashion.send("black sneakers please")
    assert model.calls == 5 and replies_to(outbox, NUMBER)
    assert any(r.getMessage() == "ai_guard.store_error" for r in caplog.records)


def test_a_ledger_failure_does_not_stop_the_counting(fashion, outbox, monkeypatch):
    def broken(self, **fields):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(UsageEventRepo, "record", broken)
    set_provider_override(Looping())
    fashion.send("black sneakers please")
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(UsageEvent)) == 0
    assert by_scope(counters(scope="tenant"))[("tenant", "hour")] == (5, 5)


def test_unmetered_and_offline_assistants_reserve_nothing(fashion, outbox):
    fashion.send("black sneakers please")  # the offline rules engine
    unmetered = Looping()
    unmetered.metered = False  # what the evaluation harness does
    set_provider_override(unmetered)
    fashion.send("black sneakers please")
    assert unmetered.calls == 5 and counters() == []


# ---------------------------------------------------------------- UTC buckets, retention, metrics
def test_hour_and_day_buckets_are_fixed_utc_buckets(fashion):
    bid = uuid.UUID(fashion.business_id)
    guard = AIGuard(engine, bid)
    utc = timezone.utc
    for at in (datetime(2026, 1, 1, 10, 0, 0, tzinfo=utc), datetime(2026, 1, 1, 10, 59, 59, tzinfo=utc),
               datetime(2026, 1, 1, 11, 0, 0, tzinfo=utc), datetime(2026, 1, 1, 23, 59, 59, tzinfo=utc),
               datetime(2026, 1, 2, 0, 0, 0, tzinfo=utc),
               datetime(2026, 1, 2, 1, 30, tzinfo=timezone(timedelta(hours=2)))):  # 23:30 UTC on 1 January
        guard.reserve_call(at=at)
    hours = {r.period_start.astimezone(utc): r.calls for r in counters(period="hour")}
    days = {r.period_start.astimezone(utc): r.calls for r in counters(period="day")}
    assert hours == {datetime(2026, 1, 1, 10, tzinfo=utc): 2, datetime(2026, 1, 1, 11, tzinfo=utc): 1,
                     datetime(2026, 1, 1, 23, tzinfo=utc): 2, datetime(2026, 1, 2, 0, tzinfo=utc): 1}
    assert days == {datetime(2026, 1, 1, tzinfo=utc): 5, datetime(2026, 1, 2, tzinfo=utc): 1}


def test_old_buckets_are_purged_and_the_ledger_is_not(fashion, outbox, db):
    set_provider_override(Looping())
    fashion.send("black sneakers please")  # current buckets, a message row, ledger rows
    bid = uuid.UUID(fashion.business_id)
    old = datetime.now(timezone.utc) - timedelta(days=10)
    AIGuard(engine, bid).reserve_call(at=old)  # an old hour and day bucket
    ledger_before = db.scalar(select(func.count()).select_from(UsageEvent))
    assert purge_ai_usage_counters(db, settings.webhook_event_retention_days) == 2
    db.commit()
    assert len(counters()) == 5 and all(r.period_start > old for r in counters() if r.period != "lifetime")
    later = datetime.now(timezone.utc) + timedelta(days=settings.webhook_event_retention_days + 1)
    assert purge_ai_usage_counters(db, settings.webhook_event_retention_days, now=later) == 5  # all of them, in time
    db.commit()
    assert db.scalar(select(func.count()).select_from(UsageEvent)) == ledger_before > 0  # the history stays


def test_metrics_show_reservations_and_over_limit(fashion, outbox, db, monkeypatch):
    tiny_limits(monkeypatch)
    set_provider_override(Looping())
    fashion.send("black sneakers please")
    text_ = metrics(db, workers_running=True)
    assert 'duka_ai_guard_reserved_current_hour{unit="calls"} 5' in text_
    assert "duka_ai_guard_busiest_tenant_calls_current_hour 5" in text_
    assert 'duka_ai_guard_over_limit_24h{scope="tenant"} 8' in text_  # hour and day rows, 4 each
    assert 'duka_ai_guard_denied_24h{scope="tenant"} 0' in text_


# ---------------------------------------------------------------- configuration
@pytest.mark.parametrize("bad", [{"ai_guard_tenant_mode": "enforce"}, {"ai_guard_message_mode": "on"},
                                 {"ai_guard_tenant_calls_per_hour": -1}])
def test_invalid_guard_settings_are_refused(bad):
    with pytest.raises(ValidationError):
        Settings(**bad)


def test_counters_reconcile_with_the_ledger(fashion, electronics, outbox, monkeypatch):
    """The documented reconciliation: per tenant and UTC hour, reserved calls >= llm_call rows in the ledger and
    reserved attempts >= their attempts; equal when no call crashed between reservation and ledger write."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    replies = [httpx.Response(503, text="busy"), completion("Which size do you wear?", usage=(80, 9))]
    set_provider_override(openai_provider(lambda req: replies.pop(0)))
    fashion.send("black sneakers please")
    set_provider_override(Looping())
    electronics.send("samsung phone")
    with SessionLocal() as s:
        for business_id in (uuid.UUID(fashion.business_id), uuid.UUID(electronics.business_id)):
            calls, attempts = s.execute(text(
                "SELECT count(*), coalesce(sum(attempts), 0) FROM usage_events "
                "WHERE business_id = :b AND kind = 'llm_call' AND occurred_at >= date_trunc('hour', now(), 'UTC')"),
                {"b": business_id}).one()
            assert by_scope(counters(scope="tenant", business_id=business_id))[("tenant", "hour")] == (calls, attempts)
