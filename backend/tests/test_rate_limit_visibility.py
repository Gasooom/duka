"""Messages over the per-customer rate limit are kept, flagged and shown to the owner, never dropped silently.

The limit itself (30 messages a minute per customer and shop) is unchanged and the customer still gets no automatic
answer beyond it (a reply could feed a bot loop). What changed: the message is marked, the conversation goes on the
owner's attention list, and the owner gets one alert per conversation per day, through the same notifications as
every other alert.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, update

from app.core.ratelimit import inbound_message_limiter
from app.models import Message, Notification, WebhookEvent
from app.workflows.inbound import RATE_LIMIT_ALERT

CUSTOMER = "250788111222"


@pytest.fixture
def limit_of_two(monkeypatch):
    monkeypatch.setattr(inbound_message_limiter, "limit", 2)


def alerts(tenant) -> list[dict]:
    return [n for n in tenant.get("/api/dashboard/notifications").json() if n["kind"] == RATE_LIMIT_ALERT]


def customer_replies(outbox) -> list[str]:
    return [body for to, body in outbox.sent if to == CUSTOMER]


def conversation(tenant) -> dict:
    [conv] = tenant.get("/api/conversations").json()
    return conv


def customer_messages(tenant, conv_id: str) -> list[dict]:
    return [m for m in tenant.get(f"/api/conversations/{conv_id}").json()["messages"] if m["role"] == "customer"]


def test_the_limit_itself_is_unchanged():
    assert (inbound_message_limiter.limit, inbound_message_limiter.window) == (30, 60.0)


def test_the_31st_message_in_a_minute_is_no_longer_silent(fashion, outbox):
    for i in range(31):
        fashion.send(f"hello {i}")
    assert len(customer_replies(outbox)) == 30
    conv = conversation(fashion)
    assert conv["needs_attention"] is True
    assert [a["entity_id"] for a in alerts(fashion)] == [conv["id"]]
    assert [m["metadata"].get("rate_limited", False) for m in customer_messages(fashion, conv["id"])][-2:] == \
        [False, True]


def test_messages_over_the_limit_are_kept_flagged_and_alerted_once(fashion, outbox, db, limit_of_two):
    fashion.patch("/api/business/settings", json={"owner_notification_phone": "+250788000999"})
    for i in range(5):
        fashion.send(f"hello {i}")
    assert len(customer_replies(outbox)) == 2  # no automatic answer beyond the limit
    conv = conversation(fashion)
    assert conv["needs_attention"] is True
    msgs = customer_messages(fashion, conv["id"])
    assert [m["content"] for m in msgs] == [f"hello {i}" for i in range(5)]  # every message is kept
    assert [m["metadata"].get("rate_limited", False) for m in msgs] == [False, False, True, True, True]
    [alert] = alerts(fashion)
    assert (alert["entity_type"], alert["entity_id"], alert["status"]) == ("conversation", conv["id"], "sent")
    assert f"(+{CUSTOMER})" in alert["body"] and "more than 2 WhatsApp messages in a minute" in alert["body"]
    assert len([body for _to, body in outbox.sent if body == alert["body"]]) == 1  # one WhatsApp alert to the owner
    db.expire_all()
    assert db.scalars(select(WebhookEvent.result).order_by(WebhookEvent.seq)).all() == \
        ["replied", "replied", "rate_limited", "rate_limited", "rate_limited"]


def test_a_long_burst_alerts_once_a_day_not_once_per_message(fashion, outbox, db, limit_of_two):
    for i in range(6):
        fashion.send(f"spam {i}")
    assert len(alerts(fashion)) == 1
    db.execute(update(Notification).where(Notification.kind == RATE_LIMIT_ALERT)
               .values(created_at=datetime.now(timezone.utc) - timedelta(hours=25)))
    db.commit()
    fashion.send("spam again")  # the same burst, a day later: the owner is reminded once
    fashion.send("and again")
    assert len(alerts(fashion)) == 2


def test_limits_and_alerts_belong_to_each_shop(fashion, electronics, outbox, limit_of_two):
    for i in range(3):
        fashion.send(f"hello {i}")
    for i in range(4):  # the same number at another shop has its own limit and its own alert
        electronics.send(f"hello {i}")
    assert len(customer_replies(outbox)) == 4
    f_conv, e_conv = conversation(fashion), conversation(electronics)
    assert [a["entity_id"] for a in alerts(fashion)] == [f_conv["id"]]
    assert [a["entity_id"] for a in alerts(electronics)] == [e_conv["id"]]
    assert fashion.get(f"/api/conversations/{e_conv['id']}").status_code == 404


def test_normal_conversations_are_unaffected(fashion, outbox, limit_of_two):
    fashion.send("hello")
    fashion.send("black sneakers under 100k")
    assert len(customer_replies(outbox)) == 2
    conv = conversation(fashion)
    assert conv["needs_attention"] is False and alerts(fashion) == []
    assert not any(m["metadata"].get("rate_limited") for m in customer_messages(fashion, conv["id"]))


def test_a_redelivered_webhook_is_neither_stored_nor_alerted_twice(fashion, outbox, db, limit_of_two):
    for i in range(2):
        fashion.send(f"hello {i}")
    fashion.send("over the limit", wa_id="wamid.redelivered")
    fashion.send("over the limit", wa_id="wamid.redelivered")  # Meta retries the same webhook
    assert len(alerts(fashion)) == 1
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Message)
                     .where(Message.wa_message_id == "wamid.redelivered")) == 1
