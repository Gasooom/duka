"""Durable WhatsApp processing: nothing is lost on a crash, nothing happens twice on a redelivery, and no reply
reaches a customer before the state it describes is committed."""
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.core.config import settings
from app.db.session import SessionLocal
from app.integrations.whatsapp.adapters import SendResult, WhatsAppAdapter, set_adapter_override
from app.integrations.whatsapp.parser import build_text_webhook
from app.main import app
from app.models import Cart, Conversation, Customer, Message, Order, WebhookEvent
from app.services.commerce_service import CartService
from app.services.conversation_service import ConversationService, CustomerService
from app.services.messaging_service import deliver_due, recover_stale_sends, send_to_customer
from app.workflows import inbound
from app.workflows.inbound import claim, ingest_and_commit, process_event, process_webhook_payload
from app.workflows.worker import BackgroundWorkers
from tests.conftest import drain

NUMBER = "250788111222"


def _events(db, **where):
    db.expire_all()
    return list(db.scalars(select(WebhookEvent).filter_by(**where).order_by(WebhookEvent.seq)))


def _make_due(db):
    db.execute(update(WebhookEvent).where(WebhookEvent.status == "retry")
               .values(next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    db.commit()


def _count(db, model, *where):
    db.expire_all()
    return db.scalar(select(func.count()).select_from(model).where(*where))


def test_message_is_persisted_before_ack_and_survives_a_crash(fashion, outbox, db):
    r = fashion.send("black sneakers", process=False)  # the process "dies" right after acknowledging
    assert r.status_code == 200 and r.json()["accepted"] == 1
    [event] = _events(db)
    assert event.status == "pending" and event.payload["text"] == "black sneakers"
    assert _count(db, Message) == 0 and outbox.sent == []
    drain()  # a restarted worker picks it up
    [event] = _events(db)
    assert event.status == "done" and event.result == "replied" and event.attempts == 1
    assert len(outbox.sent) == 1 and "Adidas Samba" in outbox.sent[0][1]


def test_ingest_failure_returns_5xx_so_meta_redelivers(fashion, outbox, db, monkeypatch):
    from app.api.routes import webhooks

    def broken(payload, session_factory=SessionLocal):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(webhooks, "ingest_and_commit", broken)
    client = TestClient(app, raise_server_exceptions=False)
    payload = build_text_webhook(fashion.phone_number_id, "+250700", NUMBER, "black sneakers", "wamid.RETRY1")
    assert client.post("/webhooks/whatsapp", json=payload).status_code == 500
    assert _events(db) == []
    monkeypatch.undo()
    assert client.post("/webhooks/whatsapp", json=payload).status_code == 200  # Meta's redelivery
    drain()
    assert len(outbox.sent) == 1


def test_failure_after_order_creation_rolls_back_everything_and_retries(fashion, outbox, db, monkeypatch):
    for t in ("black sneakers", "add 1", "deliver to Remera, KG 11 Ave"):
        fashion.send(t)
    sent = len(outbox.sent)

    def crash(*args, **kwargs):
        raise RuntimeError("crash after the agent created the order")

    monkeypatch.setattr(inbound, "send_to_customer", crash)
    fashion.send("yes", wa_id="wamid.PLACE")
    assert _count(db, Order) == 0, "order must roll back with the failed turn"
    assert _count(db, Message, Message.wa_message_id == "wamid.PLACE") == 0
    assert len(outbox.sent) == sent, "nothing may be sent for a rolled-back turn"
    [event] = _events(db, external_id="wamid.PLACE")
    assert event.status == "retry" and event.attempts == 1 and "crash after" in event.last_error

    monkeypatch.undo()
    _make_due(db)
    drain()
    assert _count(db, Order) == 1
    assert len(outbox.sent) == sent + 1 and "confirmed" in outbox.sent[-1][1]
    [event] = _events(db, external_id="wamid.PLACE")
    assert event.status == "done" and event.attempts == 2


def test_commit_failure_means_no_reply_is_sent(fashion, outbox, db):
    """The reply exists only as an outbox row of the same transaction: if that transaction fails to commit,
    the customer gets nothing (and the event is retried)."""
    commits = {"n": 0}

    def flaky_factory():
        s = SessionLocal()
        real = s.commit

        def commit():
            commits["n"] += 1
            if commits["n"] == 3:  # 1 = ingest, 2 = claim, 3 = the processing transaction
                raise RuntimeError("connection lost during commit")
            real()

        s.commit = commit
        return s

    payload = build_text_webhook(fashion.phone_number_id, "+250700", NUMBER, "black sneakers", "wamid.COMMIT")
    assert [r.status for r in process_webhook_payload(payload, flaky_factory)] == ["error"]
    assert outbox.sent == [] and _count(db, Message) == 0
    [event] = _events(db)
    assert event.status == "retry"
    _make_due(db)
    drain()
    assert len(outbox.sent) == 1


def test_expired_lease_of_a_dead_worker_is_reclaimed(fashion, outbox, db):
    fashion.send("black sneakers", process=False)
    claimed = claim()  # a worker claims the event and then dies without finishing
    assert claimed is not None
    assert drain() == []  # lease still valid: nobody else touches it
    db.execute(update(WebhookEvent).values(locked_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
    db.commit()
    assert [r.status for r in drain()] == ["replied"]
    [event] = _events(db)
    assert event.status == "done" and event.attempts == 2
    assert len(outbox.sent) == 1


def test_duplicate_and_concurrent_redeliveries_have_one_effect(fashion, outbox, db):
    payload = build_text_webhook(fashion.phone_number_id, "+250700", NUMBER, "black sneakers", "wamid.DUP")
    threads = [threading.Thread(target=ingest_and_commit, args=(payload,)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    fashion.client.post("/webhooks/whatsapp", json=payload)
    drain()
    fashion.client.post("/webhooks/whatsapp", json=payload)  # Meta retry long after processing
    drain()
    assert len(_events(db)) == 1
    assert _count(db, Message, Message.role == "customer") == 1
    assert _count(db, Customer) == 1 and _count(db, Conversation) == 1
    assert len(outbox.sent) == 1


def test_retries_then_dead_letter_and_per_customer_order(fashion, outbox, db, monkeypatch):
    real = inbound.process_message

    def poison(db_, msg):
        if msg.text == "poison":
            raise RuntimeError("cannot process")
        return real(db_, msg)

    monkeypatch.setattr(inbound, "process_message", poison)
    fashion.send("poison", wa_id="wamid.P1")
    fashion.send("black sneakers", wa_id="wamid.P2")  # same customer, later message
    assert outbox.sent == [], "a later message must not overtake an unfinished earlier one"
    for _ in range(settings.webhook_max_attempts):
        _make_due(db)
        drain()
    [p1] = _events(db, external_id="wamid.P1")
    [p2] = _events(db, external_id="wamid.P2")
    assert p1.status == "dead" and p1.attempts == settings.webhook_max_attempts
    assert p2.status == "done" and len(outbox.sent) == 1  # unblocked once P1 was dead-lettered


def test_claims_keep_per_sender_order_but_run_senders_in_parallel(fashion, outbox, db):
    for wa, number in (("w-a1", "250788000001"), ("w-a2", "250788000001"), ("w-b1", "250788000002")):
        fashion.send("black sneakers", from_number=number, wa_id=wa, process=False)
    seq = {e.external_id: e.id for e in _events(db)}
    first, second = claim(), claim()  # two workers, simultaneously
    assert (first[0], second[0]) == (seq["w-a1"], seq["w-b1"])
    assert claim() is None  # w-a2 waits for w-a1
    process_event(*first)
    assert claim()[0] == seq["w-a2"]


class FlakyAdapter(WhatsAppAdapter):
    mode = "test"

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def send_text(self, to, body):
        self.calls.append(body)
        return self.results.pop(0)


OK = SendResult(ok=True, wa_message_id="wamid.out.ok", delivery_status="sent")
TRANSIENT = SendResult(ok=False, wa_message_id=None, delivery_status="failed", error="HTTP 503", retryable=True)
PERMANENT = SendResult(ok=False, wa_message_id=None, delivery_status="failed", error="HTTP 400: bad recipient")


def _reply(db):
    db.expire_all()
    return db.scalars(select(Message).where(Message.role == "assistant")).one()


def test_transient_send_failure_is_retried_later(fashion, db):
    adapter = FlakyAdapter([TRANSIENT, OK])
    set_adapter_override(adapter)
    fashion.send("black sneakers")
    msg = _reply(db)
    assert msg.delivery_status == "retry" and msg.send_attempts == 1 and msg.next_send_at is not None
    deliver_due()
    assert len(adapter.calls) == 1, "not due yet"
    db.execute(update(Message).where(Message.id == msg.id).values(next_send_at=datetime.now(timezone.utc)))
    db.commit()
    deliver_due()
    msg = _reply(db)
    assert msg.delivery_status == "sent" and msg.send_attempts == 2 and len(adapter.calls) == 2


def test_permanent_send_failure_is_flagged_not_retried(fashion, db):
    adapter = FlakyAdapter([PERMANENT])
    set_adapter_override(adapter)
    fashion.send("black sneakers")
    msg = _reply(db)
    assert msg.delivery_status == "failed" and "bad recipient" in msg.attributes["error"]
    deliver_due()
    assert len(adapter.calls) == 1
    assert db.get(Conversation, msg.conversation_id).needs_attention is True


def test_reply_committed_but_not_sent_is_delivered_by_the_worker(fashion, outbox, db, monkeypatch):
    monkeypatch.setattr(inbound, "deliver_outbox", lambda outbox, sf=None: None)  # process dies between commit and send
    fashion.send("black sneakers")
    assert outbox.sent == [] and _reply(db).delivery_status == "queued"
    monkeypatch.undo()
    BackgroundWorkers().run_once()
    assert len(outbox.sent) == 1 and _reply(db).delivery_status == "sent"
    BackgroundWorkers().run_once()
    assert len(outbox.sent) == 1


def test_send_interrupted_midway_is_not_blindly_resent(fashion, outbox, db, monkeypatch):
    monkeypatch.setattr(inbound, "deliver_outbox", lambda outbox, sf=None: None)
    fashion.send("black sneakers")
    msg = _reply(db)
    db.execute(update(Message).where(Message.id == msg.id).values(
        delivery_status="sending", send_started_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    db.commit()
    assert recover_stale_sends() == 1
    msg = _reply(db)
    assert msg.delivery_status == "failed" and "outcome unknown" in msg.attributes["error"]
    assert db.get(Conversation, msg.conversation_id).needs_attention is True
    deliver_due()
    assert outbox.sent == []


def test_concurrent_first_contact_creates_one_customer_conversation_and_cart(fashion, db):
    bid = uuid.UUID(fashion.business_id)
    barrier = threading.Barrier(6)
    errors = []

    def first_contact():
        s = SessionLocal()
        try:
            barrier.wait()
            customer = CustomerService(s, bid).upsert_from_whatsapp("250788777000")
            conv = ConversationService(s, bid).get_or_create_active(customer)
            CartService(s, bid).get_active(customer, conv)
            s.commit()
        except Exception as exc:
            errors.append(exc)
        finally:
            s.close()

    threads = [threading.Thread(target=first_contact) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert (_count(db, Customer), _count(db, Conversation), _count(db, Cart)) == (1, 1, 1)


def test_background_workers_process_webhooks_without_help(fashion, outbox):
    workers = BackgroundWorkers()
    workers.start(2)
    try:
        fashion.send("black sneakers", process=False)
        workers.wake()
        deadline = time.monotonic() + 15
        while not outbox.sent and time.monotonic() < deadline:
            time.sleep(0.1)
    finally:
        workers.stop()
    assert len(outbox.sent) == 1 and "Adidas Samba" in outbox.sent[0][1]


def test_status_updates_never_move_backwards(fashion, outbox, db, client):
    fashion.send("black sneakers")
    msg = _reply(db)

    def status(s):
        client.post("/webhooks/whatsapp", json={"object": "whatsapp_business_account", "entry": [{"changes": [{
            "value": {"metadata": {"phone_number_id": fashion.phone_number_id},
                      "statuses": [{"id": msg.wa_message_id, "status": s, "recipient_id": NUMBER}]}}]}]})
        return _reply(db).delivery_status

    assert status("read") == "read"
    assert status("delivered") == "read"
    assert status("failed") == "failed"


def test_human_reply_reports_real_delivery_status(fashion, db):
    set_adapter_override(FlakyAdapter([OK, TRANSIENT]))
    fashion.send("I want to talk to a human")
    conv = fashion.get("/api/conversations").json()[0]
    r = fashion.post(f"/api/conversations/{conv['id']}/reply", json={"text": "Hi, this is Jane."})
    assert r.json()["delivery_status"] == "retry"


def test_queued_message_is_not_sent_if_its_transaction_rolls_back(fashion, outbox, db):
    fashion.send("black sneakers")
    bid = uuid.UUID(fashion.business_id)
    conv = db.scalars(select(Conversation)).one()
    send_to_customer(db, bid, conv, "Your order is confirmed!")
    db.rollback()
    deliver_due()
    assert all("confirmed" not in body for _, body in outbox.sent)
