"""WhatsApp's 24-hour customer-service window. Normal messages are only delivered within 24 hours of the customer's
last message, so a message to a customer who has been silent longer (an order update, payment instructions, a staff
reply) is not attempted; the owner is told once instead. When Meta accepts a message and reports it failed later,
the owner is told too. The owner must never believe a customer was informed when they were not."""
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import func, select, text

from app.db.session import SessionLocal
from app.integrations.whatsapp.adapters import (
    META_WINDOW_ERROR,
    OUTSIDE_WINDOW,
    CloudWhatsAppAdapter,
    SendResult,
    WhatsAppAdapter,
    set_adapter_override,
)
from app.integrations.whatsapp.parser import parse_webhook
from app.models import Conversation, Message, Notification
from app.services.conversation_service import ConversationService
from app.services.messaging_service import FAILED_ALERT, WINDOW_ALERT, deliver_due, send_to_customer
from tests.conftest import place_order

NUMBER = "250788111222"
OWNER = "250788999000"


def _customer_wrote_hours_ago(hours: float, business_id: str | None = None) -> None:
    with SessionLocal() as s:
        s.execute(text("UPDATE messages SET created_at = clock_timestamp() - make_interval(secs => :secs) "
                       "WHERE role = 'customer' AND (CAST(:bid AS uuid) IS NULL OR business_id = CAST(:bid AS uuid))"),
                  {"secs": hours * 3600, "bid": business_id})
        s.commit()


def _to(outbox, number: str) -> list[str]:
    return [body for to, body in outbox.sent if to == number]


def _alerts(kind: str) -> list[Notification]:
    with SessionLocal() as s:
        return list(s.scalars(select(Notification).where(Notification.kind == kind).order_by(Notification.created_at)))


def _latest_outbound() -> Message:
    with SessionLocal() as s:
        return s.scalars(select(Message).where(Message.role.in_(("assistant", "human_agent")))
                         .order_by(Message.created_at.desc())).first()


def _conversation() -> Conversation:
    with SessionLocal() as s:
        return s.scalars(select(Conversation)).one()


def _status_webhook(t, wamid: str, status: str = "failed", errors: list | None = None):
    value = {"metadata": {"phone_number_id": t.phone_number_id},
             "statuses": [{"id": wamid, "status": status, "recipient_id": NUMBER, **({"errors": errors} if errors else {})}]}
    return t.client.post("/webhooks/whatsapp", json={"object": "whatsapp_business_account",
                                                     "entry": [{"changes": [{"value": value}]}]})


def _owner_phone(t) -> None:
    assert t.patch("/api/business/settings", json={"owner_notification_phone": "+" + OWNER}).status_code == 200


def test_last_inbound_time_counts_only_the_customers_own_messages(fashion, outbox):
    fashion.send("hello", from_number=NUMBER)  # the greeting reply is an outbound message
    _customer_wrote_hours_ago(25)
    with SessionLocal() as s:
        conv = s.scalars(select(Conversation)).one()
        send_to_customer(s, conv.business_id, conv, "Your order is ready")  # queued now: also outbound
        s.commit()
        last = ConversationService(s, conv.business_id).last_inbound_at(conv.customer_id)
        conv_activity = s.get(Conversation, conv.id).last_message_at
    now = datetime.now(timezone.utc)
    assert 24.9 < (now - last).total_seconds() / 3600 < 25.1
    assert (now - conv_activity).total_seconds() < 60  # why conversation.last_message_at cannot be used


def test_silent_customer_is_not_messaged_and_the_owner_is_told_once(fashion, outbox):
    _owner_phone(fashion)
    order = place_order(fashion, NUMBER)
    sent_before = len(_to(outbox, NUMBER))
    _customer_wrote_hours_ago(25)

    assert fashion.patch(f"/api/orders/{order['id']}", json={"status": "accepted"}).status_code == 200
    assert len(_to(outbox, NUMBER)) == sent_before  # the adapter was never asked to send it
    accepted = _latest_outbound()
    assert accepted.delivery_status == "failed" and accepted.attributes["failure_reason"] == OUTSIDE_WINDOW
    assert "24-hour" in accepted.attributes["error"] and accepted.send_attempts == 1
    assert _conversation().needs_attention is True
    [alert] = _alerts(WINDOW_ALERT)
    assert alert.entity_type == "conversation" and alert.recipient == OWNER and order["order_number"] in alert.body
    assert "+" + NUMBER in alert.body and "25 hours ago" in alert.body
    assert alert.body in _to(outbox, OWNER)  # the owner alert itself goes out (to the owner's number)

    # More updates while the customer stays silent: still blocked, still one alert.
    fashion.patch(f"/api/orders/{order['id']}", json={"status": "ready"})
    conv = _conversation()
    fashion.post(f"/api/conversations/{conv.id}/handoff")
    r = fashion.post(f"/api/conversations/{conv.id}/reply", json={"text": "Hello, are you still there?"})
    assert r.json()["delivery_status"] == "failed"
    assert len(_to(outbox, NUMBER)) == sent_before and len(_alerts(WINDOW_ALERT)) == 1
    deliver_due()  # a failed message is final: never retried
    assert len(_to(outbox, NUMBER)) == sent_before

    # The customer writes again: the window reopens and messages flow; a later silence is news again.
    fashion.send("hi, is my order coming?", from_number=NUMBER)
    fashion.post(f"/api/conversations/{conv.id}/reply", json={"text": "Yes, today!"})
    assert _to(outbox, NUMBER)[-1] == "Yes, today!"
    with SessionLocal() as s:  # move the story on: the first alert came before the customer's latest message
        s.execute(text("UPDATE notifications SET created_at = created_at - interval '40 hours'"))
        s.commit()
    _customer_wrote_hours_ago(26)
    fashion.patch(f"/api/orders/{order['id']}", json={"status": "out_for_delivery"})
    assert len(_alerts(WINDOW_ALERT)) == 2


def test_messages_inside_the_window_are_sent_normally(fashion, outbox):
    order = place_order(fashion, NUMBER)
    _customer_wrote_hours_ago(23)
    fashion.patch(f"/api/orders/{order['id']}", json={"status": "accepted"})
    msg = _latest_outbound()
    assert msg.delivery_status == "sent" and "failure_reason" not in msg.attributes
    assert order["order_number"] in _to(outbox, NUMBER)[-1]
    assert _alerts(WINDOW_ALERT) == [] and _conversation().needs_attention is False


@pytest.mark.parametrize("hours, delivered", [(23.4, True), (23.6, False)])
def test_safety_margin_boundary(fashion, outbox, hours, delivered):
    order = place_order(fashion, NUMBER)
    _customer_wrote_hours_ago(hours)  # the threshold is 23.5 h (WHATSAPP_WINDOW_HOURS)
    fashion.patch(f"/api/orders/{order['id']}", json={"status": "accepted"})
    assert (_latest_outbound().delivery_status == "sent") is delivered
    assert len(_alerts(WINDOW_ALERT)) == (0 if delivered else 1)


def test_late_meta_failure_131047_is_recorded_and_the_owner_told_once(fashion, outbox):
    order = place_order(fashion, NUMBER)
    fashion.patch(f"/api/orders/{order['id']}", json={"status": "accepted"})
    accepted = _latest_outbound()
    assert accepted.delivery_status == "sent"  # Meta accepted it first...
    error = [{"code": 131047, "title": "Re-engagement message", "message": "Re-engagement message",
              "error_data": {"details": "More than 24 hours have passed since the recipient last replied."}}]
    assert _status_webhook(fashion, accepted.wa_message_id, errors=error).status_code == 200  # ...then failed it
    _status_webhook(fashion, accepted.wa_message_id, errors=error)  # Meta retries webhooks
    with SessionLocal() as s:
        msg = s.get(Message, accepted.id)
    assert msg.delivery_status == "failed" and msg.attributes["error_code"] == META_WINDOW_ERROR
    assert msg.attributes["failure_reason"] == OUTSIDE_WINDOW and "131047" in msg.attributes["error"]
    assert _conversation().needs_attention is True
    [alert] = _alerts(WINDOW_ALERT)
    assert order["order_number"] in alert.body and "Re-engagement" not in alert.body  # owner wording, not Meta's
    assert "WhatsApp reports that more than 24 hours had passed" in alert.body  # Meta's verdict, not our clock's
    fashion.patch(f"/api/orders/{order['id']}", json={"status": "ready"})  # another late failure, same silence
    _status_webhook(fashion, _latest_outbound().wa_message_id, errors=error)
    assert len(_alerts(WINDOW_ALERT)) == 1


def test_other_late_failures_are_flagged_and_reported_once(fashion, outbox):
    fashion.send("black sneakers", from_number=NUMBER)
    reply = _latest_outbound()
    _status_webhook(fashion, reply.wa_message_id, errors=[{"code": "131026", "title": "Message undeliverable"}])
    with SessionLocal() as s:
        msg = s.get(Message, reply.id)
    assert msg.attributes["failure_reason"] == "whatsapp_failed" and msg.attributes["error_code"] == 131026
    [alert] = _alerts(FAILED_ALERT)
    assert "131026" in alert.body and "Message undeliverable" in alert.body and _alerts(WINDOW_ALERT) == []
    _status_webhook(fashion, reply.wa_message_id, errors=[{"code": 131026, "title": "Message undeliverable"}])
    assert len(_alerts(FAILED_ALERT)) == 1 and _conversation().needs_attention is True


def test_late_failure_of_an_owner_alert_is_visible(fashion, outbox):
    _owner_phone(fashion)
    place_order(fashion, NUMBER)
    with SessionLocal() as s:
        alert = s.scalars(select(Notification).where(Notification.kind == "new_order")).one()
    assert alert.status == "sent" and alert.wa_message_id
    _status_webhook(fashion, alert.wa_message_id, errors=[{"code": 131047, "title": "Re-engagement message"}])
    shown = {n["id"]: n for n in fashion.get("/api/dashboard/notifications").json()}[str(alert.id)]
    assert shown["status"] == "failed" and "131047" in shown["error"] and "template" in shown["error"]
    assert _alerts(WINDOW_ALERT) == [] and _alerts(FAILED_ALERT) == []  # no alert about the alert channel itself


def test_another_shop_cannot_touch_these_rows(fashion, electronics, outbox):
    order = place_order(fashion, NUMBER)
    fashion.patch(f"/api/orders/{order['id']}", json={"status": "accepted"})
    accepted = _latest_outbound()
    # A failure for the fashion shop's message arriving on the electronics shop's number changes nothing.
    _status_webhook(electronics, accepted.wa_message_id, errors=[{"code": 131047, "title": "Re-engagement message"}])
    with SessionLocal() as s:
        assert s.get(Message, accepted.id).delivery_status == "sent"
        assert s.scalar(select(func.count()).select_from(Notification).where(
            Notification.kind.in_((WINDOW_ALERT, FAILED_ALERT)))) == 0
    # The window is per shop: the same person writing to the fashion shop does not open the electronics one.
    electronics.send("do you have phones?", from_number=NUMBER)
    _customer_wrote_hours_ago(30, electronics.business_id)
    with SessionLocal() as s:
        conv_e = s.scalars(select(Conversation).where(Conversation.business_id == uuid.UUID(electronics.business_id))).one()
        send_to_customer(s, conv_e.business_id, conv_e, "We have new phones!")
        s.commit()
    deliver_due()
    assert "We have new phones!" not in _to(outbox, NUMBER)
    [alert] = _alerts(WINDOW_ALERT)
    assert str(alert.business_id) == electronics.business_id


class WindowRejectingAdapter(WhatsAppAdapter):
    """Meta refusing a message on the spot (HTTP 400, error 131047) instead of failing it later."""
    mode = "test"

    def send_text(self, to: str, body: str) -> SendResult:
        return SendResult(ok=False, wa_message_id=None, delivery_status="failed", error="HTTP 400: (#131047)",
                          error_code=META_WINDOW_ERROR, reason=OUTSIDE_WINDOW)


def test_immediate_meta_window_rejection_is_handled_the_same_way(fashion, outbox):
    order = place_order(fashion, NUMBER)
    set_adapter_override(WindowRejectingAdapter())  # e.g. our clock and Meta's disagree by a few minutes
    fashion.patch(f"/api/orders/{order['id']}", json={"status": "accepted"})
    msg = _latest_outbound()
    assert msg.delivery_status == "failed" and msg.attributes["failure_reason"] == OUTSIDE_WINDOW
    assert msg.attributes["error_code"] == META_WINDOW_ERROR and len(_alerts(WINDOW_ALERT)) == 1
    assert "WhatsApp reports" in _alerts(WINDOW_ALERT)[0].body


def test_cloud_adapter_reads_metas_error_code():
    def handler(request):
        return httpx.Response(400, json={"error": {"message": "(#131047) Re-engagement message", "type": "OAuthException",
                                                   "code": 131047, "fbtrace_id": "x"}})
    adapter = CloudWhatsAppAdapter("pnid", "token", client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = adapter.send_text(NUMBER, "hi")
    assert (result.ok, result.retryable, result.error_code, result.reason) == (False, False, 131047, OUTSIDE_WINDOW)


def test_status_webhook_errors_are_parsed():
    _, [status] = parse_webhook({"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "p1"},
        "statuses": [{"id": "w1", "status": "failed", "errors": [{"code": 131047, "title": "Re-engagement message"}]}]}}]}]})
    assert (status.status, status.error_code, status.error_title) == ("failed", 131047, "Re-engagement message")
