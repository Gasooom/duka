"""Usage metering, WhatsApp: the same usage_events ledger, now with inbound customer messages (wa_in), attempts to send
a message to a customer (wa_out) or an alert to the owner (wa_alert), and the late failures Meta reports for them.

An inbound message and a late failure are recorded in the transaction of the state they describe; a send attempt, in
its own transaction right after the adapter returned, under the attempt number the atomic send claim handed out. The
ledger is insert-only: a correction is a new event. Nothing here stores a phone number: `market` is a country calling
code. Prices come from the operator's file; the amounts below are arbitrary test numbers, not anyone's price list."""
import dataclasses
import hashlib
import hmac
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DataError, IntegrityError

from app.core.config import settings
from app.db.session import SessionLocal, engine
from app.integrations.whatsapp.adapters import (
    CloudWhatsAppAdapter,
    SendResult,
    WhatsAppAdapter,
    set_adapter_override,
)
from app.integrations.whatsapp.market import CALLING_CODES, calling_code
from app.models import Conversation, Message, Notification, UsageEvent, WebhookEvent
from app.repositories.repos import UsageEventRepo
from app.services import pricing
from app.services.messaging_service import deliver, deliver_due, notify_owner, recover_stale_sends, send_to_customer
from app.services.usage_service import WA_OUT, record_wa_send
from app.workflows import inbound
from tests.conftest import CapturingAdapter, drain, place_order

NUMBER = "250788111222"   # a customer in Rwanda: calling code 250
OWNER = "250788999000"


# ---------------------------------------------------------------- helpers
def wa(kind: str | None = None, **where) -> list[UsageEvent]:
    """The WhatsApp events of the ledger, oldest first (never the AI ones)."""
    with SessionLocal() as s:
        q = select(UsageEvent).where(UsageEvent.kind != "llm_call").filter_by(**where)
        if kind:
            q = q.where(UsageEvent.kind == kind)
        return list(s.scalars(q.order_by(UsageEvent.occurred_at)))


def ok() -> SendResult:
    return SendResult(ok=True, wa_message_id=f"wamid.out.{uuid.uuid4().hex[:12]}", delivery_status="sent")


TRANSIENT = SendResult(ok=False, wa_message_id=None, delivery_status="failed", error="HTTP 503", retryable=True)
PERMANENT = SendResult(ok=False, wa_message_id=None, delivery_status="failed", error="HTTP 400: bad recipient")


class Scripted(WhatsAppAdapter):
    """An adapter that sends for real (as far as the ledger can tell) and answers with the scripted results in order;
    the last one repeats."""
    mode = "cloud-test"

    def __init__(self, *results: SendResult, is_real: bool = True, delay: float = 0.0):
        self.results, self.is_real, self.delay = list(results), is_real, delay
        self.calls: list[tuple] = []

    def _next(self) -> SendResult:
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        # every accepted message gets its own id from WhatsApp, even when the script repeats a result
        return dataclasses.replace(result, wa_message_id=f"wamid.out.{uuid.uuid4().hex[:12]}") if result.ok else result

    def send_text(self, to: str, body: str) -> SendResult:
        self.calls.append(("text", to, body))
        time.sleep(self.delay)
        return self._next()

    def send_template(self, to: str, name: str, language: str, params: list[str]) -> SendResult:
        self.calls.append(("template", to, name))
        return self._next()


def use(adapter: WhatsAppAdapter) -> WhatsAppAdapter:
    set_adapter_override(adapter)
    return adapter


def assistant(db) -> Message:
    db.expire_all()
    return db.scalars(select(Message).where(Message.role == "assistant").order_by(Message.created_at.desc())).first()


def make_due(db, model, row_id) -> None:
    db.execute(update(model).where(model.id == row_id).values(next_send_at=datetime.now(timezone.utc)))
    db.commit()


def post(t, message: dict, *, secret: str | None = None, number: str = NUMBER):
    """A WhatsApp webhook with one message, in Meta's shape, signed when a secret is given."""
    value = {"messaging_product": "whatsapp", "metadata": {"display_phone_number": "+250700",
                                                           "phone_number_id": t.phone_number_id},
             "contacts": [{"profile": {"name": "Customer"}, "wa_id": number}],
             "messages": [{"from": number, "timestamp": "0", **message}]}
    return _post(t, {"object": "whatsapp_business_account", "entry": [{"id": "W", "changes": [
        {"field": "messages", "value": value}]}]}, secret)


def post_status(t, wamid: str, *, error_code: int = 131047, secret: str | None = None):
    value = {"metadata": {"phone_number_id": t.phone_number_id},
             "statuses": [{"id": wamid, "status": "failed", "recipient_id": NUMBER,
                           "errors": [{"code": error_code, "title": "Re-engagement message"}]}]}
    return _post(t, {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": value}]}]}, secret)


def _post(t, payload: dict, secret: str | None):
    raw = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    r = t.client.post("/webhooks/whatsapp", content=raw, headers=headers)
    drain()
    return r


def owner_phone(t, **extra) -> None:
    assert t.patch("/api/business/settings", json={"owner_notification_phone": "+" + OWNER, **extra}).status_code == 200


def new_alert(t, body: str = "Order KF-1 placed") -> uuid.UUID:
    with SessionLocal() as s:
        n = notify_owner(s, uuid.UUID(t.business_id), "new_order", body)
        s.commit()
        return n.id


def queue_reply(db, text_: str = "Your order is ready") -> uuid.UUID:
    conv = db.execute(text("SELECT id, business_id FROM conversations")).one()
    with SessionLocal() as s:
        msg = send_to_customer(s, conv.business_id, s.get(Conversation, conv.id), text_)
        s.commit()
        return msg.id


def customer_wrote_hours_ago(hours: float) -> None:
    with SessionLocal() as s:
        s.execute(text("UPDATE messages SET created_at = clock_timestamp() - make_interval(secs => :s) "
                       "WHERE role = 'customer'"), {"s": hours * 3600})
        s.commit()


@pytest.fixture(autouse=True)
def no_price_list(monkeypatch):
    monkeypatch.setattr(settings, "usage_pricing_file", "")  # a developer's .env must not change these tests


@pytest.fixture
def prices(tmp_path, monkeypatch):
    def install(whatsapp: dict | None = None, *, version="2026-10-09", raw: str | None = None):
        path = tmp_path / f"prices-{uuid.uuid4().hex[:8]}.json"
        path.write_text(raw if raw is not None else json.dumps(
            {"version": version, "currency": "USD", **({"whatsapp": whatsapp} if whatsapp is not None else {})}),
            encoding="utf-8")
        monkeypatch.setattr(settings, "usage_pricing_file", str(path))
    return install


# ---------------------------------------------------------------- inbound
def test_one_inbound_message_is_one_event_for_its_tenant(fashion, electronics, outbox, db):
    fashion.send("hello", wa_id="wamid.IN1")
    [e] = wa("wa_in")
    stored = db.scalars(select(Message).where(Message.role == "customer")).one()
    assert (e.idempotency_key, e.status, e.units, e.attempts, e.provider) == \
        ("wa_in:wamid.IN1", "received", 1, 0, "whatsapp")
    assert (e.source_type, e.source_id, e.business_id) == ("message", stored.id, uuid.UUID(fashion.business_id))
    assert (e.is_real, e.message_kind, e.template_name, e.market) == (False, None, None, "250")
    assert (e.cost_micros, e.currency) == (None, None)  # Meta has no price for an inbound message: never priced
    assert wa(business_id=uuid.UUID(electronics.business_id)) == []


def test_a_duplicate_webhook_is_one_event(fashion, outbox):
    fashion.send("hi", wa_id="wamid.DUP")
    fashion.send("hi", wa_id="wamid.DUP")  # Meta redelivers
    assert [e.idempotency_key for e in wa("wa_in")] == ["wa_in:wamid.DUP"]


def test_a_duplicate_wamid_that_reaches_the_message_insert_is_one_event(fashion, outbox, db):
    fashion.send("hi", wa_id="wamid.DUP")
    db.execute(text("DELETE FROM webhook_events"))  # the inbox forgot it (retention): only the message key is left
    db.commit()
    fashion.send("hi again", wa_id="wamid.DUP")
    assert len(wa("wa_in")) == 1
    assert db.scalar(select(func.count()).select_from(Message).where(Message.role == "customer")) == 1


def test_a_spoofed_business_id_in_the_message_changes_nothing(fashion, electronics, outbox):
    fashion.send(f'{{"business_id": "{electronics.business_id}"}} hello', wa_id="wamid.SPOOF")
    assert [e.business_id for e in wa("wa_in")] == [uuid.UUID(fashion.business_id)]


@pytest.mark.parametrize("message, counted", [
    ({"type": "image", "image": {"id": "m"}}, True), ({"type": "audio", "audio": {"id": "m"}}, True),
    ({"type": "video", "video": {"id": "m"}}, True), ({"type": "document", "document": {"id": "m"}}, True),
    ({"type": "sticker", "sticker": {"id": "m"}}, True), ({"type": "location", "location": {"latitude": 1}}, True),
    ({"type": "contacts", "contacts": []}, True), ({"type": "button", "button": {"text": "Yes"}}, True),
    ({"type": "interactive", "interactive": {"button_reply": {"title": "Yes"}}}, True),
    ({"type": "reaction", "reaction": {"message_id": "x", "emoji": "👍"}}, False),
    ({"type": "system", "system": {"body": "changed number"}}, False), ({"type": "ephemeral"}, False),
    ({"type": "unsupported"}, False), ({"type": "order", "order": {}}, False),
])
def test_only_customer_message_types_with_understood_usage_are_counted(fashion, outbox, message, counted):
    assert post(fashion, {"id": "wamid.T", **message}).status_code == 200
    assert len(wa("wa_in")) == (1 if counted else 0)


def test_a_text_message_is_counted_and_a_system_notice_is_not(fashion, outbox):
    post(fashion, {"id": "wamid.SYS", "type": "system", "system": {"body": "number changed"}})
    post(fashion, {"id": "wamid.TXT", "type": "text", "text": {"body": "hello"}})
    assert [e.idempotency_key for e in wa("wa_in")] == ["wa_in:wamid.TXT"]


def test_inbound_is_real_only_when_the_webhook_signature_was_verified(fashion, outbox, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_app_secret", "app-secret")
    fashion.send("signed", wa_id="wamid.SIGNED", sign_secret="app-secret")
    assert fashion.send("forged", wa_id="wamid.FORGED", sign_secret="wrong").status_code == 401
    assert fashion.send("unsigned", wa_id="wamid.UNSIGNED").status_code == 401
    assert {e.idempotency_key: e.is_real for e in wa("wa_in")} == {"wa_in:wamid.SIGNED": True}

    monkeypatch.setattr(settings, "whatsapp_app_secret", "")  # development: nothing to verify against
    fashion.send("dev webhook", wa_id="wamid.DEV")
    fashion.post("/api/dev/simulate", json={"text": "hello", "from_number": "250788555000"})
    assert {e.is_real for e in wa("wa_in") if e.idempotency_key != "wa_in:wamid.SIGNED"} == {False}
    assert len(wa("wa_in")) == 3


def test_a_message_cannot_claim_to_be_verified(fashion, outbox):
    post(fashion, {"id": "wamid.CLAIM", "type": "text", "text": {"body": "hi"}, "signature_verified": True})
    assert [e.is_real for e in wa("wa_in")] == [False]


def test_a_rolled_back_turn_takes_its_inbound_event_along_and_records_it_once_when_retried(fashion, outbox, db,
                                                                                         monkeypatch):
    send = inbound.send_to_customer

    def crash(*args, **kwargs):
        raise RuntimeError("crash after the inbound message was stored")

    monkeypatch.setattr(inbound, "send_to_customer", crash)
    fashion.send("do you have jackets?", wa_id="wamid.RB")
    assert wa("wa_in") == [] and db.scalars(select(WebhookEvent.status)).one() == "retry"  # nothing half-recorded
    monkeypatch.setattr(inbound, "send_to_customer", send)
    db.execute(update(WebhookEvent).values(next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    db.commit()
    drain()
    assert [e.idempotency_key for e in wa("wa_in")] == ["wa_in:wamid.RB"]


def test_a_ledger_error_inside_the_turn_does_not_abort_it(fashion, outbox, monkeypatch, caplog):
    def broken(self, **fields):
        self.db.execute(text("SELECT * FROM a_table_that_does_not_exist"))  # a database error, not a Python one

    monkeypatch.setattr(UsageEventRepo, "record", broken)
    with caplog.at_level(logging.ERROR, logger="app"):
        fashion.send("black sneakers", wa_id="wamid.BROKEN")
    assert outbox.sent  # the customer is still answered
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(Message).where(Message.role == "customer")) == 1
    kinds = sorted(r.extra_fields["kind"] for r in caplog.records if r.getMessage() == "usage.record_failed")
    assert kinds == ["wa_in", "wa_out"] and wa() == []


# ---------------------------------------------------------------- customer messages: the send attempt
def test_a_real_send_is_one_real_free_form_success(fashion, db):
    adapter = use(Scripted(ok()))
    fashion.send("black sneakers")
    reply = assistant(db)
    [e] = wa("wa_out")
    assert len(adapter.calls) == 1 and reply.delivery_status == "sent"
    assert (e.idempotency_key, e.status, e.units, e.attempts) == (f"wa_out:{reply.id}:1", "success", 1, 1)
    assert (e.source_type, e.source_id, e.provider) == ("message", reply.id, "whatsapp")
    assert (e.is_real, e.message_kind, e.template_name, e.market) == (True, "free_form", None, "250")
    assert e.business_id == uuid.UUID(fashion.business_id)


def test_a_simulated_send_is_recorded_as_not_real(fashion, db):
    fashion.send("black sneakers")  # no adapter hook: the development adapter of a dev account
    reply = assistant(db)
    [e] = wa("wa_out")
    assert reply.delivery_status == "simulated"
    assert (e.status, e.is_real, e.message_kind) == ("success", False, "free_form")


def test_the_test_capture_adapter_is_metered_but_never_real(fashion, outbox):
    fashion.send("black sneakers")
    assert [(e.status, e.is_real) for e in wa("wa_out")] == [("success", False)]


def test_a_transient_failure_and_its_retry_are_two_attempts(fashion, db):
    adapter = use(Scripted(TRANSIENT, ok()))
    fashion.send("black sneakers")
    reply = assistant(db)
    assert reply.delivery_status == "retry"
    [first] = wa("wa_out")
    assert (first.idempotency_key, first.status, first.attempts, first.is_real) == \
        (f"wa_out:{reply.id}:1", "failed", 1, True)
    make_due(db, Message, reply.id)
    deliver_due()
    deliver_due()  # nothing left to send
    assert assistant(db).delivery_status == "sent" and len(adapter.calls) == 2
    assert [(e.idempotency_key, e.status, e.attempts) for e in wa("wa_out")] == \
        [(f"wa_out:{reply.id}:1", "failed", 1), (f"wa_out:{reply.id}:2", "success", 2)]


def test_a_permanent_failure_is_one_failed_attempt_and_is_not_retried(fashion, db):
    adapter = use(Scripted(PERMANENT))
    fashion.send("black sneakers")
    reply = assistant(db)
    deliver_due()
    assert reply.delivery_status == "failed" and len(adapter.calls) == 1
    assert [(e.status, e.attempts) for e in wa("wa_out")] == [("failed", 1)]


def test_an_adapter_that_raises_is_a_failed_attempt(fashion, db):
    class Raises(Scripted):
        def send_text(self, to, body):
            raise ConnectionError("network edge")

    use(Raises(ok()))
    fashion.send("black sneakers")
    assert assistant(db).delivery_status == "retry"
    assert [(e.status, e.is_real) for e in wa("wa_out")] == [("failed", True)]


def test_nothing_is_recorded_when_a_closed_window_means_nothing_was_attempted(fashion, db):
    adapter = use(Scripted(ok()))
    fashion.send("hello", from_number=NUMBER)
    before = len(adapter.calls), len(wa("wa_out"))
    customer_wrote_hours_ago(25)
    late = queue_reply(db)
    deliver_due()
    msg = db.get(Message, late)
    db.refresh(msg)
    assert msg.delivery_status == "failed" and msg.attributes["failure_reason"] == "outside_24h_window"
    assert (len(adapter.calls), len(wa("wa_out"))) == before and wa(source_id=late) == []


def test_nothing_is_recorded_without_an_active_account(fashion, db):
    adapter = use(Scripted(ok()))
    fashion.send("hello", from_number=NUMBER)
    db.execute(text("UPDATE whatsapp_accounts SET is_active = false"))
    db.commit()
    late = queue_reply(db)
    deliver_due()
    db.expire_all()
    assert "No WhatsApp account" in db.get(Message, late).attributes["error"]
    assert len(adapter.calls) == 1 and wa(source_id=late) == []


def test_nothing_is_recorded_by_an_adapter_that_sends_nothing(fashion, db):
    fashion.send("hello", from_number=NUMBER)  # development adapter: answered
    db.execute(text("UPDATE whatsapp_accounts SET mode = 'cloud', access_token_encrypted = NULL"))
    db.commit()
    before = len(wa("wa_out"))
    late = queue_reply(db)
    deliver_due()  # cloud account without a token: the misconfigured adapter fails loudly instead of sending
    db.expire_all()
    assert "token missing" in db.get(Message, late).attributes["error"]
    assert len(wa("wa_out")) == before and wa(source_id=late) == []


def test_concurrent_workers_make_one_attempt_and_one_event(fashion, db, monkeypatch):
    monkeypatch.setattr(inbound, "deliver_outbox", lambda outbox, sf=None: None)  # the reply stays queued
    adapter = use(Scripted(ok(), delay=0.3))
    fashion.send("black sneakers")
    reply = assistant(db)
    assert reply.delivery_status == "queued"
    barrier = threading.Barrier(6)

    def work():
        barrier.wait()
        deliver([reply.id])

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(adapter.calls) == 1 and assistant(db).send_attempts == 1
    assert [e.idempotency_key for e in wa("wa_out")] == [f"wa_out:{reply.id}:1"]


def _account_older_than_the_claim(db) -> None:
    """The claims below are faked as an hour old; the account must not have been changed since."""
    db.execute(text("UPDATE whatsapp_accounts SET updated_at = now() - interval '2 hours'"))
    db.commit()


def _strand(db, msg_id, attempts: int) -> None:
    _account_older_than_the_claim(db)
    db.execute(update(Message).where(Message.id == msg_id).values(
        delivery_status="sending", send_attempts=attempts, send_started_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    db.commit()


def test_an_interrupted_send_is_unknown_under_its_own_attempt_number(fashion, db, monkeypatch):
    monkeypatch.setattr(inbound, "deliver_outbox", lambda outbox, sf=None: None)
    use(Scripted(ok()))
    fashion.send("black sneakers")
    reply = assistant(db)
    _strand(db, reply.id, attempts=2)  # the second attempt died midway
    assert recover_stale_sends() == 1
    reply = assistant(db)
    assert reply.delivery_status == "failed" and reply.send_attempts == settings.outbox_max_attempts
    [e] = wa("wa_out")
    assert (e.idempotency_key, e.status, e.attempts, e.is_real, e.message_kind, e.market) == \
        (f"wa_out:{reply.id}:2", "unknown", 2, True, "free_form", "250")  # the number before it was overwritten
    assert recover_stale_sends() == 0 and len(wa("wa_out")) == 1


def test_a_result_the_attempt_did_write_wins_over_unknown(fashion, db, monkeypatch):
    monkeypatch.setattr(inbound, "deliver_outbox", lambda outbox, sf=None: None)
    use(Scripted(ok()))
    fashion.send("black sneakers")
    reply = assistant(db)
    # The worker got its answer and recorded it, then died before saving the message's own state.
    assert record_wa_send(engine, reply.business_id, kind=WA_OUT, source_type="message", source_id=reply.id, attempt=1,
                          status="success", is_real=True, message_kind="free_form", template_name=None,
                          recipient=NUMBER)
    _strand(db, reply.id, attempts=1)
    assert recover_stale_sends() == 1
    assert [(e.idempotency_key, e.status) for e in wa("wa_out")] == [(f"wa_out:{reply.id}:1", "success")]


def _account_changed_after_the_claim(db) -> None:
    db.execute(text("UPDATE whatsapp_accounts SET updated_at = now()"))  # e.g. switched from dev to cloud mode
    db.commit()


def test_realness_of_an_interrupted_send_is_not_taken_from_an_account_changed_since_the_claim(fashion, db,
                                                                                            monkeypatch):
    monkeypatch.setattr(inbound, "deliver_outbox", lambda outbox, sf=None: None)
    use(Scripted(ok()))  # the account says "real" now; nobody can tell what it said when the send was claimed
    fashion.send("black sneakers")
    reply = assistant(db)
    _strand(db, reply.id, attempts=1)
    _account_changed_after_the_claim(db)
    assert recover_stale_sends() == 1
    [e] = wa("wa_out")
    assert (e.idempotency_key, e.status, e.is_real, e.cost_micros) == (f"wa_out:{reply.id}:1", "unknown", None, None)


def test_an_alert_interrupted_after_the_account_changed_is_not_classified(fashion, db):
    use(Scripted(ok()))
    owner_phone(fashion)
    nid = new_alert(fashion)
    db.execute(update(Notification).where(Notification.id == nid).values(
        status="sending", attempts=1, send_started_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    db.commit()
    _account_changed_after_the_claim(db)  # the account was touched after the claim of an hour ago
    assert recover_stale_sends() == 1
    assert [(e.status, e.is_real) for e in wa("wa_alert")] == [("unknown", None)]


def test_a_recorded_result_stays_authoritative_when_the_account_changes_before_recovery(fashion, db, monkeypatch):
    monkeypatch.setattr(inbound, "deliver_outbox", lambda outbox, sf=None: None)
    use(Scripted(ok()))
    fashion.send("black sneakers")
    reply = assistant(db)
    assert record_wa_send(engine, reply.business_id, kind=WA_OUT, source_type="message", source_id=reply.id, attempt=1,
                          status="failed", is_real=True, message_kind="free_form", template_name=None,
                          recipient=NUMBER)
    _strand(db, reply.id, attempts=1)
    db.execute(text("UPDATE whatsapp_accounts SET mode = 'dev', updated_at = now()"))
    db.commit()
    use(CapturingAdapter())  # today's configuration says "not real"
    recover_stale_sends()
    [e] = wa("wa_out")
    assert (e.status, e.is_real) == ("failed", True)  # what the attempt itself recorded, untouched


def test_a_late_failure_does_not_read_an_unknown_realness_as_simulated(fashion, db):
    use(Scripted(ok()))
    fashion.send("black sneakers")
    reply = assistant(db)
    other = uuid.uuid4()  # a message whose interrupted attempt could not be classified
    assert record_wa_send(engine, reply.business_id, kind=WA_OUT, source_type="message", source_id=other, attempt=1,
                          status="unknown", is_real=None, message_kind="free_form", template_name=None,
                          recipient=NUMBER)
    from app.services.usage_service import record_wa_late_failure
    assert record_wa_late_failure(db, reply.business_id, kind=WA_OUT, source_type="message", source_id=other,
                                  attempt=1, verified=True, message_kind="free_form", recipient=NUMBER)
    db.commit()
    [late] = wa("wa_out", idempotency_key=f"wa_late_fail:{other}")
    assert (late.status, late.is_real, late.units) == ("late_failed", True, 0)


def test_an_interrupted_send_of_a_dev_account_is_unknown_and_not_real(fashion, db, monkeypatch):
    monkeypatch.setattr(inbound, "deliver_outbox", lambda outbox, sf=None: None)
    fashion.send("black sneakers")  # the development adapter
    reply = assistant(db)
    _strand(db, reply.id, attempts=1)
    recover_stale_sends()
    assert [(e.status, e.is_real) for e in wa("wa_out")] == [("unknown", False)]


# ---------------------------------------------------------------- owner alerts
def test_a_free_form_alert_is_recorded_without_inventing_template_data(fashion, outbox, db):
    owner_phone(fashion)
    nid = new_alert(fashion)
    deliver_due()
    [e] = wa("wa_alert")
    assert (e.idempotency_key, e.status, e.attempts) == (f"wa_alert:{nid}:1", "success", 1)
    assert (e.source_type, e.source_id, e.is_real) == ("notification", nid, False)
    assert (e.message_kind, e.template_name, e.market) == ("free_form", None, "250")
    assert db.get(Notification, nid).status == "sent" and (OWNER, "Order KF-1 placed") in outbox.sent


def test_a_real_template_alert_keeps_the_template_name(fashion, db):
    adapter = use(Scripted(ok()))
    owner_phone(fashion, owner_notification_template="new_order_alert")
    nid = new_alert(fashion)
    deliver_due()
    [e] = wa("wa_alert")
    assert adapter.calls == [("template", OWNER, "new_order_alert")]
    assert (e.source_id, e.is_real, e.message_kind, e.template_name) == (nid, True, "template", "new_order_alert")


def test_an_alert_that_is_not_sent_is_not_recorded(fashion, outbox):
    new_alert(fashion)  # no owner phone: recorded for the dashboard, status skipped
    deliver_due()
    assert wa("wa_alert") == [] and outbox.sent == []


def test_an_alert_retry_is_a_second_attempt(fashion, db):
    adapter = use(Scripted(TRANSIENT, ok()))
    owner_phone(fashion)
    nid = new_alert(fashion)
    deliver_due()
    assert db.get(Notification, nid).status == "retry"
    make_due(db, Notification, nid)
    deliver_due()
    assert len(adapter.calls) == 2
    assert [(e.idempotency_key, e.status, e.attempts) for e in wa("wa_alert")] == \
        [(f"wa_alert:{nid}:1", "failed", 1), (f"wa_alert:{nid}:2", "success", 2)]


def test_an_interrupted_alert_is_unknown_and_claims_nothing_about_what_it_was(fashion, db):
    use(Scripted(ok()))
    owner_phone(fashion, owner_notification_template="new_order_alert")
    nid = new_alert(fashion)
    _account_older_than_the_claim(db)
    db.execute(update(Notification).where(Notification.id == nid).values(
        status="sending", attempts=1, send_started_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    db.commit()
    assert recover_stale_sends() == 1
    [e] = wa("wa_alert")
    assert (e.idempotency_key, e.status, e.is_real, e.message_kind, e.template_name) == \
        (f"wa_alert:{nid}:1", "unknown", True, None, None)


def test_real_alerts_of_an_order_use_the_orders_notification_ids(fashion, db):
    use(Scripted(ok()))
    owner_phone(fashion)
    place_order(fashion, NUMBER)
    db.expire_all()
    sent_alerts = {n.id for n in db.scalars(select(Notification)) if n.status == "sent"}
    assert sent_alerts and {e.source_id for e in wa("wa_alert")} == sent_alerts


# ---------------------------------------------------------------- late failures
def test_a_late_failure_is_a_new_event_that_corrects_without_a_second_send(fashion, db):
    use(Scripted(ok()))
    fashion.send("black sneakers")
    reply = assistant(db)
    [sent] = wa("wa_out")
    assert post_status(fashion, reply.wa_message_id).status_code == 200
    assert post_status(fashion, reply.wa_message_id).status_code == 200  # Meta retries webhooks
    events = wa("wa_out")
    assert [(e.idempotency_key, e.status, e.units) for e in events] == \
        [(f"wa_out:{reply.id}:1", "success", 1), (f"wa_late_fail:{reply.id}", "late_failed", 0)]
    late = events[1]
    assert (events[0].id, events[0].status) == (sent.id, "success")  # the original event is untouched
    assert (late.source_type, late.source_id, late.attempts) == ("message", reply.id, 1)
    assert (late.is_real, late.message_kind, late.market) == (True, "free_form", "250")  # what the attempt recorded
    assert sum(e.units for e in events) == 1  # still one send


def test_a_late_failure_of_an_alert_repeats_the_template_of_the_attempt(fashion, db):
    use(Scripted(ok()))
    owner_phone(fashion, owner_notification_template="new_order_alert")
    nid = new_alert(fashion)
    deliver_due()
    wamid = db.get(Notification, nid).wa_message_id
    post_status(fashion, wamid)
    post_status(fashion, wamid)
    sent, late = wa("wa_alert")
    assert (late.idempotency_key, late.status, late.units, late.source_type) == \
        (f"wa_late_fail:{nid}", "late_failed", 0, "notification")
    assert (late.is_real, late.message_kind, late.template_name, late.market) == (True, "template", "new_order_alert", "250")
    assert sent.status == "success"


def test_a_late_failure_without_a_recorded_attempt_uses_what_is_known_now(fashion, db, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_app_secret", "app-secret")
    use(Scripted(ok()))
    record = UsageEventRepo.record

    def broken(self, **fields):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(UsageEventRepo, "record", broken)
    fashion.send("black sneakers", sign_secret="app-secret")  # sent, but its event was lost
    monkeypatch.setattr(UsageEventRepo, "record", record)
    assert wa() == []
    reply = assistant(db)
    post_status(fashion, reply.wa_message_id, secret="app-secret")  # a signed status webhook: really from Meta
    [late] = wa("wa_out")
    assert (late.idempotency_key, late.status, late.units, late.is_real, late.message_kind, late.market) == \
        (f"wa_late_fail:{reply.id}", "late_failed", 0, True, "free_form", "250")


def test_another_shops_number_cannot_report_a_failure_for_these_messages(fashion, electronics, db):
    use(Scripted(ok()))
    fashion.send("black sneakers")
    reply = assistant(db)
    post_status(electronics, reply.wa_message_id)
    assert [e.status for e in wa("wa_out")] == ["success"]


# ---------------------------------------------------------------- isolation and the ledger's rules
def test_each_tenant_sees_only_its_own_whatsapp_events(fashion, electronics, outbox, db):
    a, b = uuid.UUID(fashion.business_id), uuid.UUID(electronics.business_id)
    fashion.send("hello", wa_id="wamid.A")
    electronics.send("hello", wa_id="wamid.B")
    owner_phone(fashion)
    new_alert(fashion)
    deliver_due()
    a_repo, b_repo = UsageEventRepo(db, a), UsageEventRepo(db, b)
    a_events, b_events = a_repo.list(), b_repo.list()
    assert {e.kind for e in a_events} == {"wa_in", "wa_out", "wa_alert"} and {e.kind for e in b_events} == {"wa_in", "wa_out"}
    assert {e.business_id for e in a_events} == {a} and {e.business_id for e in b_events} == {b}
    assert a_repo.get(b_events[0].id) is None and b_repo.get(a_events[0].id) is None
    # A caller-supplied business id is ignored, whatever the kind.
    assert a_repo.record(business_id=b, kind="wa_in", idempotency_key="wa_in:wamid.SPOOF", status="received",
                         is_real=False, units=1)
    db.commit()
    assert [e.idempotency_key for e in b_repo.list() if "SPOOF" in e.idempotency_key] == []
    assert [e.business_id for e in a_repo.list() if "SPOOF" in e.idempotency_key] == [a]
    with pytest.raises(IntegrityError, match="immutable"):
        db.execute(text("UPDATE usage_events SET business_id = :b WHERE kind = 'wa_in' AND business_id = :a"),
                   {"a": a, "b": b})
    db.rollback()


@pytest.mark.parametrize("sql, error", [
    ("UPDATE usage_events SET is_real = NOT is_real WHERE kind = 'wa_in'", "append-only"),
    ("UPDATE usage_events SET market = '1' WHERE kind = 'wa_in'", "append-only"),
    ("UPDATE usage_events SET status = 'late_failed' WHERE kind = 'wa_out'", "append-only"),
    ("UPDATE usage_events SET units = 0 WHERE kind = 'wa_out'", "append-only"),
    ("DELETE FROM usage_events WHERE kind = 'wa_in'", "append-only"),
    ("DELETE FROM usage_events", "append-only"),
])
def test_whatsapp_events_can_never_be_changed_or_removed(fashion, outbox, db, sql, error):
    fashion.send("hello")
    before = len(wa())
    with pytest.raises(IntegrityError, match=error):
        db.execute(text(sql))
    db.rollback()
    assert len(wa()) == before >= 2


def _fields(**over) -> dict:
    base = dict(kind="wa_out", idempotency_key=f"k:{uuid.uuid4().hex}", status="success", is_real=True, units=1)
    return {**base, **over}


@pytest.mark.parametrize("fields, constraint", [
    (_fields(kind="wa_in", status="received", is_real=None), "ck_usage_events_wa_fields"),
    (_fields(kind="llm_call", is_real=False), "ck_usage_events_wa_fields"),
    (_fields(kind="llm_call", is_real=None, market="250"), "ck_usage_events_wa_fields"),
    (_fields(kind="wa_in", status="success"), "ck_usage_events_wa_status"),
    (_fields(status="received"), "ck_usage_events_wa_status"),
    (_fields(status="success", is_real=None), "ck_usage_events_wa_fields"),  # only an unknown outcome may lack it
    (_fields(status="late_failed", is_real=None), "ck_usage_events_wa_fields"),
    (_fields(kind="wa_late_fail"), "ck_usage_events_kind"),
    (_fields(message_kind="marketing"), "ck_usage_events_message_kind"),
    (_fields(message_kind="free_form", template_name="new_order_alert"), "ck_usage_events_template"),
    (_fields(template_name="new_order_alert"), "ck_usage_events_template"),
    (_fields(market="025"), "ck_usage_events_market"),  # a calling code never starts with 0
    (_fields(market="+25"), "ck_usage_events_market"),
    (_fields(market="ab"), "ck_usage_events_market"),
])
def test_the_ledger_refuses_inconsistent_whatsapp_events(fashion, db, fields, constraint):
    with pytest.raises(IntegrityError, match=constraint):
        UsageEventRepo(db, uuid.UUID(fashion.business_id)).record(**fields)
        db.flush()
    db.rollback()


@pytest.mark.parametrize("market", ["250788111222", "0788111222", "+250788111222", "2507"])
def test_a_phone_number_can_never_be_stored_as_a_market(fashion, db, market):
    """The column holds three characters at most: a calling code, never a number."""
    with pytest.raises(DataError, match="too long"):
        UsageEventRepo(db, uuid.UUID(fashion.business_id)).record(**_fields(market=market))
    db.rollback()


def test_the_ledger_accepts_every_valid_whatsapp_shape(fashion, db):
    repo = UsageEventRepo(db, uuid.UUID(fashion.business_id))
    for fields in (_fields(kind="wa_in", status="received", is_real=False, market="1"),
                   _fields(status="late_failed", units=0, message_kind="template", template_name="t", market="254"),
                   _fields(kind="wa_alert", status="unknown", is_real=False),
                   _fields(kind="wa_out", status="unknown", is_real=None),
                   _fields(kind="llm_call", is_real=None, provider="p")):
        assert repo.record(**fields)
    db.commit()


# ---------------------------------------------------------------- metering switches
def test_an_unmetered_adapter_records_neither_directions(fashion):
    class Unmetered(CapturingAdapter):
        metered = False

    use(Unmetered())  # what the evaluation harness uses: platform activity, never a tenant's usage
    fashion.send("black sneakers")
    assert wa() == []


def test_a_metered_adapter_records_both_directions(fashion, outbox):
    fashion.send("black sneakers")
    assert sorted(e.kind for e in wa()) == ["wa_in", "wa_out"]


def test_http_attempts_are_reported_by_the_cloud_adapter(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)

    def adapter(*statuses):
        codes = list(statuses)
        return CloudWhatsAppAdapter("P", "T", client=httpx.Client(transport=httpx.MockTransport(
            lambda req: httpx.Response(codes.pop(0), json={"messages": [{"id": "wamid.x"}]}))))

    assert adapter(200).send_text("1", "x").http_attempts == 1
    third = adapter(503, 503, 200).send_text("1", "x")
    assert (third.ok, third.http_attempts) == (True, 3)
    assert adapter(500, 500, 500).send_text("1", "x").http_attempts == 3
    assert adapter(400).send_text("1", "x").http_attempts == 1  # permanent: not retried
    assert adapter(200).send_template("1", "t", "en", ["x"]).http_attempts == 1


# ---------------------------------------------------------------- market
@pytest.mark.parametrize("digits, code", [
    ("250788111222", "250"), ("254712345678", "254"), ("14155550123", "1"), ("442079460123", "44"),
    ("447700900123", "44"), ("33612345678", "33"), ("919876543210", "91"), ("2348012345678", "234"),
    ("27821234567", "27"), ("79161234567", "7"), ("249912345678", "249"), ("971501234567", "971"),
])
def test_the_market_is_the_longest_matching_calling_code(digits, code):
    assert calling_code(digits) == code


@pytest.mark.parametrize("digits", [
    None, "", "0788111222", "788", "+250788111222", "250 788 111 222", "25078811122233445", "abcdefghij",
    "2100000000",  # 210 is assigned to no country
    "8001234567", "6711234567",  # no code starts 800; 671 (Guam) belongs to NANP-style numbers we do not list
    "788123456",  # a Rwandan local number without its 0 begins with 7 but is not a +7 number
    "7881234567", "1415555012", "141555501234",  # +1 and +7 numbers have exactly 10 digits after the code
])
def test_a_number_that_cannot_be_resolved_safely_has_no_market(digits):
    assert calling_code(digits) is None


def test_calling_codes_are_a_prefix_free_table_of_one_to_three_digits():
    assert all(c.isdigit() and 1 <= len(c) <= 3 and c[0] != "0" for c in CALLING_CODES)
    for a in CALLING_CODES:
        assert not any(b != a and b.startswith(a) for b in CALLING_CODES), f"{a} is a prefix of another code"
    assert {"1", "7", "20", "250", "254", "255", "256", "257", "234", "27"} <= CALLING_CODES


def test_the_market_of_a_local_owner_number_is_not_guessed(fashion, outbox):
    owner_phone(fashion)
    fashion.patch("/api/business/settings", json={"owner_notification_phone": "0788999000"})  # a local number
    nid = new_alert(fashion)
    deliver_due()
    [e] = wa("wa_alert", source_id=nid)
    assert e.market is None


# ---------------------------------------------------------------- pricing configuration
ARBITRARY = {  # test numbers only
    "billable_statuses": ["success"],
    "templates": [{"name": "new_order_alert", "category": "utility"}],
    "rules": [{"markets": ["250", "254"], "message_kind": "free_form", "price_per_message": "0.01"},
              {"markets": ["250"], "message_kind": "template", "category": "utility", "price_per_message": "0.02"}],
}


def quote(**over):
    base = dict(kind="wa_out", status="success", is_real=True, message_kind="free_form", template_name=None,
                market="250")
    return pricing.whatsapp_price(**{**base, **over})


def test_without_a_price_list_everything_is_unpriced():
    assert quote() == pricing.UNPRICED


def test_a_price_list_without_whatsapp_rules_leaves_whatsapp_unpriced(prices):
    prices(None)
    assert (quote().cost_micros, quote().price_version) == (None, "2026-10-09")


def test_rules_price_a_billable_send_by_market_kind_and_declared_category(prices):
    prices(ARBITRARY)
    assert (quote().cost_micros, quote().currency, quote().price_version) == (10_000, "USD", "2026-10-09")
    assert quote(market="254").cost_micros == 10_000
    assert quote(message_kind="template", template_name="new_order_alert").cost_micros == 20_000
    for unpriced in (quote(market="256"),  # no rule for that market
                     quote(market=None),  # market unknown
                     quote(message_kind=None),
                     quote(message_kind="template", template_name="not_declared"),  # category unknown: not guessed
                     quote(message_kind="template", template_name="new_order_alert", market="254"),
                     quote(status="unknown"),  # delivered or not, nobody knows
                     quote(is_real=None),  # an attempt that cannot be classified is not priced
                     quote(kind="wa_in", status="received", message_kind=None)):
        assert (unpriced.cost_micros, unpriced.currency, unpriced.price_version) == (None, None, "2026-10-09")


def test_what_is_not_a_billable_send_costs_nothing(prices):
    prices(ARBITRARY)
    for free in (quote(status="failed"),  # not in billable_statuses
                 quote(is_real=False),  # nothing reached WhatsApp
                 quote(status="late_failed", message_kind=None, market=None)):  # a correction, never a send
        assert (free.cost_micros, free.currency, free.price_version) == (0, "USD", "2026-10-09")
    prices({**ARBITRARY, "billable_statuses": ["success", "failed"]})
    assert quote(status="failed").cost_micros == 10_000  # the operator's choice


def test_cost_rounds_half_to_even_in_millionths(prices):
    prices({"billable_statuses": ["success"], "rules": [
        {"markets": ["250"], "message_kind": "free_form", "price_per_message": "0.0000005"},
        {"markets": ["254"], "message_kind": "free_form", "price_per_message": "0.0000015"}]})
    assert (quote().cost_micros, quote(market="254").cost_micros) == (0, 2)


_RULE = {"markets": ["250"], "message_kind": "free_form", "price_per_message": "0.01"}


@pytest.mark.parametrize("whatsapp, reason", [
    ({"rules": []}, "billable_statuses"),
    ({"billable_statuses": ["delivered"]}, "billable_statuses"),
    ({"billable_statuses": ["unknown"]}, "billable_statuses"),
    ({"billable_statuses": [], "rules": [{**_RULE, "message_kind": "template"}]}, "needs a category"),
    ({"billable_statuses": [], "rules": [{**_RULE, "category": "utility"}]}, "must not have one"),
    ({"billable_statuses": [], "rules": [{**_RULE, "markets": ["+250"]}]}, "markets"),
    ({"billable_statuses": [], "rules": [{**_RULE, "markets": ["0250"]}]}, "markets"),
    ({"billable_statuses": [], "rules": [{**_RULE, "markets": ["2500"]}]}, "markets"),
    ({"billable_statuses": [], "rules": [{**_RULE, "markets": []}]}, "markets"),
    ({"billable_statuses": [], "rules": [{**_RULE, "price_per_message": "-1"}]}, "greater than or equal to 0"),
    ({"billable_statuses": [], "rules": [{**_RULE, "price_per_msg": "1"}]}, "price_per_msg"),  # a typo is refused
    ({"billable_statuses": [], "rules": [_RULE, {**_RULE, "markets": ["254", "250"]}]}, "priced twice"),
    ({"billable_statuses": [], "templates": [{"name": "t", "category": "a"}, {"name": "t", "category": "b"}]},
     "listed twice"),
    ({"billable_statuses": [], "currency": "USD"}, "currency"),
])
def test_an_invalid_whatsapp_price_list_is_refused_with_the_reason(prices, whatsapp, reason):
    prices(whatsapp)
    with pytest.raises(pricing.PricingError) as exc:
        pricing.current_price_list()
    assert reason in str(exc.value) and "USAGE_PRICING_FILE" in str(exc.value)


def test_only_the_billable_attempt_of_a_retried_send_is_priced_and_the_price_version_is_kept(fashion, db, prices):
    prices(ARBITRARY, version="2026-10")
    use(Scripted(TRANSIENT, ok()))
    fashion.send("black sneakers")
    reply = assistant(db)
    make_due(db, Message, reply.id)
    deliver_due()
    failed, success = wa("wa_out")
    assert [(e.status, e.cost_micros, e.currency, e.price_version) for e in (failed, success)] == \
        [("failed", 0, "USD", "2026-10"), ("success", 10_000, "USD", "2026-10")]
    post_status(fashion, assistant(db).wa_message_id)
    late = wa("wa_out")[-1]
    assert (late.status, late.cost_micros) == ("late_failed", 0)
    [inbound_event] = wa("wa_in")
    assert (inbound_event.cost_micros, inbound_event.price_version) == (None, "2026-10")

    prices({**ARBITRARY, "rules": [{**_RULE, "markets": ["250", "254"], "price_per_message": "0.05"}]}, version="2026-11")
    fashion.send("and hoodies?")
    newest = wa("wa_out")[-1]
    assert (newest.cost_micros, newest.price_version) == (50_000, "2026-11")
    assert [(e.cost_micros, e.price_version) for e in wa("wa_out")[:2]] == [(0, "2026-10"), (10_000, "2026-10")]


def test_a_simulated_send_costs_nothing_when_prices_exist(fashion, outbox, prices):
    prices(ARBITRARY)
    fashion.send("black sneakers")
    assert [(e.is_real, e.cost_micros) for e in wa("wa_out")] == [(False, 0)]


def test_an_unusable_price_list_never_loses_the_event(fashion, outbox, monkeypatch):
    def broken(**kwargs):
        raise pricing.PricingError("price list unreadable")

    monkeypatch.setattr(pricing, "whatsapp_price", broken)
    fashion.send("black sneakers")
    assert sorted((e.kind, e.cost_micros) for e in wa()) == [("wa_in", None), ("wa_out", None)]
