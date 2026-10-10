"""Runaway Conversation Guard, enforcement (docs/P1_RUNAWAY_GUARD.md B3-B5, decisions D1-D6).

B3: the per-message budget (the durable webhook event, across processing retries). A refused model call or provider
attempt ends the turn as `limited`: the customer gets the facts the tools already returned or a reply that claims
nothing and offers a person, the conversation is flagged, the owner is alerted at most once per tenant and window.
Deterministic commerce paths (the YES that places an order, talking to a person, owner order updates) never depend on
the guard. If the counters cannot be trusted, an enforcing scope refuses (fail closed, D1)."""
import threading
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import func, select, text

from app.agents.providers import LLMProvider, LLMResponse, ToolCall, set_provider_override
from app.core.config import Settings, settings
from app.db.session import SessionLocal, engine
from app.i18n import t
from app.models import AgentRun, Conversation, Customer, Notification, Order, Payment, UsageEvent, WebhookEvent
from app.repositories.repos import UsageEventRepo
from app.services import ai_guard
from app.services.ai_guard import STORE_UNAVAILABLE, AIGuard, AIGuardDenied
from app.workflows import inbound
from tests.conftest import ADDRESS, drain
from tests.test_ai_guard import Looping, _crash_after_the_model, _make_retries_due, by_scope, counters
from tests.test_usage_metering import openai_provider

NUMBER = "250788111222"
LIMITED = t("ai_limited", "en") + t("ask_person", "en")


@pytest.fixture
def enforce(monkeypatch):
    """Enforce the per-message budget; optionally set its limits (calls, attempts)."""
    def install(calls: int = 0, attempts: int = 0):
        monkeypatch.setattr(settings, "ai_guard_message_mode", "enforce")
        monkeypatch.setattr(settings, "ai_guard_message_calls", calls)
        monkeypatch.setattr(settings, "ai_guard_message_attempts", attempts)
    return install


def store_down(monkeypatch) -> None:
    def broken(*a, **k):
        raise RuntimeError("counter store unavailable")
    monkeypatch.setattr(ai_guard, "_upsert", broken)


def replies(outbox, number: str = NUMBER) -> list[str]:
    return [b for to, b in outbox.sent if to == number]


def last_run() -> AgentRun:
    with SessionLocal() as s:
        return s.scalars(select(AgentRun).order_by(AgentRun.created_at.desc())).first()


def conversation(number: str = NUMBER) -> Conversation:
    with SessionLocal() as s:
        return s.scalars(select(Conversation).join(Customer, Customer.id == Conversation.customer_id)
                         .where(Customer.whatsapp_number == number)).one()


def alerts() -> list[Notification]:
    with SessionLocal() as s:
        return list(s.scalars(select(Notification).where(Notification.kind == "assistant_limited")))


def count(model) -> int:
    with SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(model))


# ---------------------------------------------------------------- exact boundaries, concurrency
def test_exactly_n_calls_are_reserved_and_the_next_is_refused(fashion, enforce):
    enforce(calls=3)
    guard = AIGuard(engine, uuid.UUID(fashion.business_id), event_id=uuid.uuid4())
    for _ in range(3):
        guard.reserve_call()
    with pytest.raises(AIGuardDenied) as refused:
        guard.reserve_call()
    assert (refused.value.scope, refused.value.period, refused.value.reason) == ("message", "lifetime", "calls")
    [row] = counters(scope="message")
    assert (row.calls, row.attempts, row.denied) == (3, 3, 1)  # the refused call left no trace but the refusal


def test_exactly_n_attempts_are_reserved_and_the_next_is_refused(fashion, enforce):
    enforce(calls=10, attempts=4)
    guard = AIGuard(engine, uuid.UUID(fashion.business_id), event_id=uuid.uuid4())
    guard.reserve_call()  # attempt 1
    assert [guard.reserve_attempt(n) for n in (2, 3, 4, 5)] == [True, True, True, False]
    assert guard.take_denial().reason == "attempts" and guard.take_denial() is None
    with pytest.raises(AIGuardDenied, match="attempts"):
        guard.reserve_call()  # a new call needs an attempt too
    [row] = counters(scope="message")
    assert (row.calls, row.attempts, row.denied) == (1, 4, 2)


def test_racing_workers_never_pass_the_limit(fashion, enforce):
    enforce(calls=5)
    bid, event = uuid.UUID(fashion.business_id), uuid.uuid4()
    granted, refused = [], []
    barrier = threading.Barrier(8)

    def work():
        guard = AIGuard(engine, bid, event_id=event)
        barrier.wait()
        for _ in range(3):
            try:
                guard.reserve_call()
                granted.append(1)
            except AIGuardDenied:
                refused.append(1)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(60)
    [row] = counters(scope="message")
    assert (len(granted), len(refused)) == (5, 19) and (row.calls, row.denied) == (5, 19)


# ---------------------------------------------------------------- the per-message budget in a conversation
def test_a_runaway_turn_stops_at_the_message_budget_and_sends_the_facts(fashion, outbox, enforce):
    enforce(calls=3)
    model = Looping()
    set_provider_override(model)
    fashion.send("black sneakers please")
    run = last_run()
    assert model.calls == 3 and run.status == "limited" and run.llm_calls == 3
    assert [s for s in run.steps if s["type"] == "guard"] == [
        {"type": "guard", "scope": "message", "period": "lifetime", "reason": "calls"}]
    assert replies(outbox)[-1].startswith("Here's what I found")  # the searches' facts, not an invented answer
    assert conversation().needs_attention is True
    [alert] = alerts()  # no owner phone in this shop: recorded for the dashboard (skipped), never lost silently
    assert alert.entity_id == conversation().id and "AI budget" in alert.body


def test_a_retry_cannot_reset_or_evade_the_message_budget(fashion, outbox, monkeypatch, enforce):
    enforce()  # default budget: one processing attempt = 1 summary + AGENT_MAX_TOOL_ITERATIONS calls
    model = Looping()
    set_provider_override(model)
    _crash_after_the_model(monkeypatch)
    fashion.send("black sneakers please", wa_id="wamid.BUDGET")
    for _ in range(settings.webhook_max_attempts + 1):
        _make_retries_due()
        drain()
    with SessionLocal() as s:
        assert s.scalars(select(WebhookEvent.status)).one() == "dead"
    calls, _ = settings.ai_guard_message_limits
    assert model.calls == calls == 6  # was 25 without the budget (5 attempts x 5 calls)
    [row] = counters(scope="message")
    assert row.calls == 6 and row.denied >= settings.webhook_max_attempts - 1


def test_provider_retries_cannot_pass_the_attempt_budget(fashion, outbox, monkeypatch, enforce):
    enforce(calls=5, attempts=2)
    monkeypatch.setattr("time.sleep", lambda s: None)
    sent = []

    def busy(req):
        sent.append(req)
        return httpx.Response(503, text="overloaded")

    set_provider_override(openai_provider(busy, max_attempts=3))
    fashion.send("black sneakers please")
    assert len(sent) == 2  # the adapter would have tried 3 times; the third was refused before it was sent
    run = last_run()
    assert run.status == "limited" and "attempts" in run.error and replies(outbox)[-1] == LIMITED
    with SessionLocal() as s:
        ledger = s.scalars(select(UsageEvent).where(UsageEvent.kind == "llm_call")).one()
    assert (ledger.status, ledger.attempts) == ("error", 2)  # what was really sent, as the ledger has it


def test_a_ledger_failure_does_not_switch_enforcement_off(fashion, outbox, monkeypatch, enforce):
    enforce(calls=2)

    def broken(self, **fields):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(UsageEventRepo, "record", broken)
    model = Looping()
    set_provider_override(model)
    fashion.send("black sneakers please")
    assert model.calls == 2 and last_run().status == "limited"


def test_one_shops_spent_budget_never_limits_another(fashion, electronics, outbox, monkeypatch, enforce):
    enforce(calls=2)
    model = Looping()
    set_provider_override(model)
    fashion.send("black sneakers please")
    electronics.send("samsung phone")
    assert model.calls == 4  # two each: each message has its own budget, each shop its own rows
    rows = counters(scope="message")
    assert sorted(str(r.business_id) for r in rows) == sorted([fashion.business_id, electronics.business_id])


# ---------------------------------------------------------------- counters unavailable: fail closed
def test_counters_unavailable_means_no_ai_call_and_no_claim(fashion, outbox, monkeypatch, enforce):
    enforce()
    store_down(monkeypatch)
    model = Looping()
    set_provider_override(model)
    fashion.send("black sneakers please")
    run = last_run()
    assert model.calls == 0 and run.status == "limited" and STORE_UNAVAILABLE in run.error
    assert replies(outbox) == [LIMITED]  # claims nothing: no product, price, order or payment
    assert conversation().needs_attention is True and count(Order) == 0
    [alert] = alerts()
    assert "usage check is unavailable" in alert.body


def test_observe_mode_never_fails_closed(fashion, outbox, monkeypatch):
    monkeypatch.setattr(settings, "ai_guard_message_mode", "observe")
    store_down(monkeypatch)  # observe mode: the counters are not trusted to refuse anything
    model = Looping()
    set_provider_override(model)
    fashion.send("black sneakers please")
    assert model.calls == 5 and last_run().status != "limited"


# ---------------------------------------------------------------- commerce stays deterministic while AI is limited
def test_orders_payments_and_people_stay_safe_while_ai_is_limited(fashion, outbox, monkeypatch, enforce):
    for step in ("black sneakers under 100k", "add 1", f"deliver to {ADDRESS}"):  # the offline engine: a summary
        fashion.send(step)
    enforce()
    store_down(monkeypatch)  # from here the guard refuses every model call
    model = Looping()
    set_provider_override(model)

    fashion.send("yes")  # the explicit YES to the delivered summary: no model involved
    [order] = fashion.get("/api/orders").json()
    assert order["status"] == "pending" and order["payment_status"] == "unpaid"
    fashion.send("yes")  # a second YES places nothing
    assert len(fashion.get("/api/orders").json()) == 1 and order["order_number"] in replies(outbox)[-1]

    fashion.send("do you have jackets too?")  # needs the model: refused, and nothing is claimed
    assert replies(outbox)[-1] == LIMITED

    assert fashion.patch(f"/api/orders/{order['id']}", json={"status": "accepted"}).status_code == 200
    assert order["order_number"] in replies(outbox)[-1]  # the owner's update reaches the customer as before

    fashion.send("I want to talk to a person")
    assert fashion.get("/api/conversations").json()[0]["status"] == "human"  # the handoff happened for real
    assert model.calls == 0
    assert fashion.get(f"/api/orders/{order['id']}").json()["payment_status"] == "unpaid"
    assert count(Payment) == 0  # no payment was recorded, so none can have been reported as made


def test_a_checkout_prepared_before_the_limit_is_still_sent_and_still_needs_yes(fashion, outbox, enforce):
    for step in ("black sneakers under 100k", "add 1"):  # the offline engine fills the cart
        fashion.send(step)
    enforce(calls=1)

    class Prepares(LLMProvider):
        name = "prepares"

        def complete(self, messages, tools, **kw):
            return LLMResponse(content=None, tool_calls=[ToolCall("c1", "prepare_checkout",
                                                                  {"delivery_address": ADDRESS})])

    set_provider_override(Prepares())
    fashion.send("I'll take it, deliver to Remera")
    assert last_run().status == "limited" and "Total" in replies(outbox)[-1]  # the server's own summary
    assert count(Order) == 0  # nothing ordered without the customer's YES
    fashion.send("yes")
    assert count(Order) == 1


# ---------------------------------------------------------------- the owner alert, the uncertainty streak
def test_the_owner_is_alerted_at_most_once_per_window(fashion, outbox, enforce):
    enforce(calls=1)
    set_provider_override(Looping())
    fashion.send("black sneakers please", from_number="250788000011")
    fashion.send("black sneakers please", from_number="250788000012")
    assert len(alerts()) == 1  # one alert for the hour, however many conversations were stopped
    assert conversation("250788000011").needs_attention and conversation("250788000012").needs_attention


def test_racing_alert_claims_have_one_winner(fashion):
    bid = uuid.UUID(fashion.business_id)
    wins, barrier = [], threading.Barrier(8)

    def claim():
        barrier.wait()
        wins.append(AIGuard(engine, bid).claim_alert("hour"))

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(30)
    assert sorted(wins) == [False] * 7 + [True]
    assert AIGuard(engine, bid).claim_alert("day") is True  # a daily limit has its own window


def test_a_guard_stop_does_not_count_as_an_unreliable_answer(fashion, outbox, enforce, db):
    set_provider_override(Looping())
    fashion.send("hello there, black sneakers?")  # an ordinary turn creates the conversation
    db.execute(text("UPDATE conversations SET state = jsonb_set(state, '{unsure_streak}', '1')"))
    db.commit()
    enforce(calls=1)
    fashion.send("black sneakers please")
    assert last_run().status == "limited"
    assert conversation().state.get("unsure_streak") == 1  # untouched: no handoff because of a guard stop


# ---------------------------------------------------------------- operator actions, switching off
def test_requeue_dead_gives_the_message_a_fresh_budget(fashion, outbox, monkeypatch, enforce):
    from app.cli import main
    enforce()
    model = Looping()
    set_provider_override(model)
    send = inbound.send_to_customer
    _crash_after_the_model(monkeypatch)
    fashion.send("black sneakers please", wa_id="wamid.REQUEUE")
    for _ in range(settings.webhook_max_attempts + 1):
        _make_retries_due()
        drain()
    assert model.calls == 6 and counters(scope="message")[0].calls == 6
    monkeypatch.setattr(inbound, "send_to_customer", send)  # the bug is fixed and deployed
    assert main(["requeue-dead"]) == 0
    drain()
    assert model.calls == 11 and replies(outbox)  # five new calls: a fresh budget, and the customer is answered
    assert counters(scope="message")[0].calls == 5


def test_switching_enforcement_off_restores_calls_and_keeps_the_history(fashion, outbox, monkeypatch, enforce):
    enforce(calls=1)
    model = Looping()
    set_provider_override(model)
    fashion.send("black sneakers please", from_number="250788000021")
    ledger_before = count(UsageEvent)
    monkeypatch.setattr(settings, "ai_guard_message_mode", "observe")  # the rollback switch
    fashion.send("black sneakers please", from_number="250788000022")
    assert model.calls == 1 + 5
    assert count(UsageEvent) > ledger_before  # the ledger only grows
    assert sorted(r.calls for r in counters(scope="message")) == [1, 5] and \
        sum(r.denied for r in counters(scope="message")) == 1  # the refusal is still on record


def test_the_message_budget_is_enforced_by_default():
    assert Settings().ai_guard_message_mode == "enforce"  # its limit never stops a first processing attempt
    assert (Settings().ai_guard_customer_mode, Settings().ai_guard_tenant_mode) == ("observe", "observe")


def test_a_counter_row_cannot_be_moved_to_another_shop(fashion, electronics, db, enforce):
    enforce(calls=1)
    AIGuard(engine, uuid.UUID(fashion.business_id), event_id=uuid.uuid4()).reserve_call()
    with pytest.raises(Exception, match="immutable"):
        db.execute(text("UPDATE ai_usage_counters SET business_id = :b"), {"b": electronics.business_id})
    db.rollback()


def test_reserved_before_known_an_unanswered_attempt_still_counts(fashion, outbox, monkeypatch, enforce):
    """A timeout after sending may still be billed: the attempt was reserved before it went out."""
    enforce(calls=5, attempts=1)
    monkeypatch.setattr("time.sleep", lambda s: None)
    sent = []

    def timeout(req):
        sent.append(req)
        raise httpx.ReadTimeout("no answer", request=req)

    set_provider_override(openai_provider(timeout, max_attempts=3))
    fashion.send("black sneakers please")
    assert len(sent) == 1 and by_scope(counters(scope="message"))[("message", "lifetime")] == (1, 1)
    assert last_run().status == "limited"


# ---------------------------------------------------------------- B4: per customer, durable
@pytest.fixture
def per_customer(monkeypatch):
    """Enforce per-customer limits (calls per UTC hour / day); attempts left unlimited unless given."""
    def install(per_hour: int = 0, per_day: int = 0, attempts_per_hour: int = 0):
        monkeypatch.setattr(settings, "ai_guard_customer_mode", "enforce")
        monkeypatch.setattr(settings, "ai_guard_customer_calls_per_hour", per_hour)
        monkeypatch.setattr(settings, "ai_guard_customer_calls_per_day", per_day)
        monkeypatch.setattr(settings, "ai_guard_customer_attempts_per_hour", attempts_per_hour)
    return install


def test_a_customer_gets_exactly_n_calls_an_hour(fashion, per_customer):
    per_customer(per_hour=3)
    bid, alice, bob = uuid.UUID(fashion.business_id), uuid.uuid4(), uuid.uuid4()
    for _ in range(3):
        AIGuard(engine, bid, customer_id=alice).reserve_call()
    with pytest.raises(AIGuardDenied) as refused:
        AIGuard(engine, bid, customer_id=alice).reserve_call()  # a new guard: as another worker or process would
    assert (refused.value.scope, refused.value.period, refused.value.reason) == ("customer", "hour", "calls")
    AIGuard(engine, bid, customer_id=bob).reserve_call()  # another customer of the same shop is not affected
    rows = {r.subject_id: r for r in counters(scope="customer", period="hour")}
    assert (rows[alice].calls, rows[alice].denied, rows[bob].calls) == (3, 1, 1)


def test_the_hourly_limit_resets_each_utc_hour_and_the_daily_limit_does_not(fashion, per_customer):
    per_customer(per_hour=2, per_day=3)
    guard = AIGuard(engine, uuid.UUID(fashion.business_id), customer_id=uuid.uuid4())
    utc = timezone.utc
    guard.reserve_call(at=datetime(2026, 3, 1, 10, 0, tzinfo=utc))
    guard.reserve_call(at=datetime(2026, 3, 1, 10, 20, tzinfo=utc))
    with pytest.raises(AIGuardDenied, match="customer hour"):
        guard.reserve_call(at=datetime(2026, 3, 1, 10, 59, 59, tzinfo=utc))
    guard.reserve_call(at=datetime(2026, 3, 1, 11, 0, tzinfo=utc))  # a new UTC hour
    with pytest.raises(AIGuardDenied, match="customer day"):
        guard.reserve_call(at=datetime(2026, 3, 1, 11, 1, tzinfo=utc))  # but the day's three are used
    guard.reserve_call(at=datetime(2026, 3, 2, 0, 0, tzinfo=utc))  # a new UTC day


def test_racing_turns_of_one_customer_never_pass_the_limit(fashion, per_customer):
    per_customer(per_hour=4)
    bid, customer = uuid.UUID(fashion.business_id), uuid.uuid4()
    granted, barrier = [], threading.Barrier(8)

    def work():
        guard = AIGuard(engine, bid, customer_id=customer, event_id=uuid.uuid4())  # eight different messages
        barrier.wait()
        try:
            guard.reserve_call()
            granted.append(1)
        except AIGuardDenied:
            pass

    threads = [threading.Thread(target=work) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(60)
    [row] = counters(scope="customer", period="hour")
    assert len(granted) == row.calls == 4 and row.denied == 4
    assert sum(r.calls for r in counters(scope="message")) == 4  # refused reservations left nothing behind


def test_a_busy_customer_is_stopped_and_the_others_are_served(fashion, outbox, per_customer):
    from app.core.ratelimit import inbound_message_limiter
    per_customer(per_hour=5)  # exactly one runaway turn's worth per customer and hour
    model = Looping()
    set_provider_override(model)
    busy, other = "250788000031", "250788000032"
    fashion.send("black sneakers please", from_number=busy)  # five calls: the customer's whole hour
    assert model.calls == 5 and last_run().status != "limited"
    fashion.send("and jackets?", from_number=busy)
    assert model.calls == 5 and last_run().status == "limited" and replies(outbox, busy)[-1] == LIMITED
    inbound_message_limiter.reset()  # a restart, or another instance: the in-memory limiter forgets...
    fashion.send("and hoodies?", from_number=busy)
    assert model.calls == 5 and replies(outbox, busy)[-1] == LIMITED  # ...the guard does not
    fashion.send("black sneakers please", from_number=other)
    assert model.calls == 5 + 5 and last_run().status != "limited"  # the other customer has their own five
    assert conversation(busy).needs_attention and not conversation(other).needs_attention
    [alert] = alerts()  # one alert for the hour
    assert "a customer reached the assistant's usage limit for this hour" in alert.body


def test_a_customer_limit_on_attempts_stops_provider_retries(fashion, outbox, monkeypatch, per_customer):
    per_customer(attempts_per_hour=2)
    monkeypatch.setattr("time.sleep", lambda s: None)
    sent = []

    def busy(req):
        sent.append(req)
        return httpx.Response(503, text="overloaded")

    set_provider_override(openai_provider(busy, max_attempts=3))
    fashion.send("black sneakers please")
    assert len(sent) == 2 and last_run().status == "limited" and "customer hour attempts" in last_run().error


def test_customer_enforcement_is_available():
    assert Settings(ai_guard_customer_mode="enforce").ai_guard_customer_mode == "enforce"
