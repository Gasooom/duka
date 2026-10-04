"""M6: business hours, after-hours expectations and the business-wide AI pause."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models import AgentRun, Notification
from app.services import hours
from tests.conftest import place_order

HOURS = {"Mon-Sat": "08:00-20:00", "Sun": "closed"}
OWNER = "250788000999"
# Kigali is UTC+2. 2026-10-05 is a Monday.
MON_10H = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)      # Mon 10:00 Kigali: open
MON_22H = datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)     # Mon 22:00: closed, opens tomorrow 08:00
SAT_21H = datetime(2026, 10, 10, 19, 0, tzinfo=timezone.utc)    # Sat 21:00: closed, Sunday closed -> Mon 08:00


@pytest.fixture
def clock(monkeypatch):
    def set_time(t):
        monkeypatch.setattr(hours, "_now", lambda: t)
    return set_time


@pytest.mark.parametrize("now,expected", [(MON_10H, True), (MON_22H, False), (SAT_21H, False)])
def test_open_now_in_the_business_timezone(now, expected):
    assert hours.is_open(HOURS, "Africa/Kigali", now) is expected


def test_next_opening_skips_closed_days():
    assert hours.next_opening(HOURS, "Africa/Kigali", MON_22H) == "tomorrow at 08:00"
    assert hours.next_opening(HOURS, "Africa/Kigali", SAT_21H) == "Mon at 08:00"
    assert hours.closed_until(HOURS, "Africa/Kigali", MON_10H) is None


@pytest.mark.parametrize("spec,day,minute,open_", [
    ({"Daily": "7am-9pm"}, 6, 20 * 60 + 30, True),
    ({"Weekdays": "08:00-12:00, 14:00-18:00"}, 2, 13 * 60, False),
    ({"Weekdays": "08:00-12:00, 14:00-18:00"}, 2, 15 * 60, True),
    ({"Fri-Sat": "18:00-02:00"}, 6, 60, True),   # Saturday night bar open until 02:00 on Sunday
    ({"Mon, Wed": "24h"}, 2, 3 * 60, True),
])
def test_hour_formats(spec, day, minute, open_):
    now = datetime(2026, 10, 5 + day, minute // 60, minute % 60, tzinfo=hours.ZoneInfo("Africa/Kigali"))
    assert hours.is_open(spec, "Africa/Kigali", now) is open_


def test_unreadable_hours_are_rejected_on_save(fashion):
    r = fashion.patch("/api/business", json={"business_hours": {"Mon-Sat": "from morning till late"}})
    assert r.status_code == 422 and "Business hours" in r.json()["detail"]
    assert fashion.patch("/api/business", json={"business_hours": {"Funday": "08:00-20:00"}}).status_code == 422
    assert fashion.patch("/api/business", json={"timezone": "Mars/Olympus"}).status_code == 422
    assert fashion.patch("/api/business", json={"business_hours": HOURS}).status_code == 200


def test_unknown_hours_make_no_claim():
    assert hours.is_open({}, "Africa/Kigali") is None and hours.closed_until(None, "Africa/Kigali") is None


def test_after_hours_handoff_says_when_the_team_is_back(fashion, outbox, clock):
    fashion.patch("/api/business", json={"business_hours": HOURS})
    clock(SAT_21H)
    fashion.send("I want to talk to a person")
    assert "closed right now" in outbox.sent[-1][1] and "Mon at 08:00" in outbox.sent[-1][1]
    assert fashion.get("/api/conversations").json()[0]["status"] == "human"


def test_open_hours_handoff_says_shortly(fashion, outbox, clock):
    fashion.patch("/api/business", json={"business_hours": HOURS})
    clock(MON_10H)
    fashion.send("I want to talk to a person")
    assert "reply here shortly" in outbox.sent[-1][1]


def test_after_hours_voice_note(fashion, outbox, client, clock):
    from tests.test_whatsapp import _media
    fashion.patch("/api/business", json={"business_hours": HOURS})
    clock(MON_22H)
    _media(fashion, client, "audio", "wvoice-night")
    assert "voice notes" in outbox.sent[-1][1] and "tomorrow at 08:00" in outbox.sent[-1][1]


def test_order_placed_after_hours_sets_expectations(fashion, outbox, clock):
    fashion.patch("/api/business", json={"business_hours": HOURS})
    clock(MON_22H)
    place_order(fashion)
    assert "closed right now and will review it when it opens (tomorrow at 08:00)" in outbox.sent[-1][1]


def test_open_now_is_a_tool_fact(fashion, outbox, db, clock):
    import uuid

    from app.models import Business, Customer
    from app.services.conversation_service import ConversationService
    from app.tools.registry import ToolContext, execute_tool
    fashion.patch("/api/business", json={"business_hours": HOURS})
    fashion.send("hello there friend")
    biz = db.get(Business, uuid.UUID(fashion.business_id))
    customer = db.scalars(select(Customer)).one()
    ctx = ToolContext(db=db, business=biz, customer=customer,
                      conversation=ConversationService(db, biz.id).get_or_create_active(customer))
    clock(MON_22H)
    info, _ = execute_tool(ctx, "get_business_information", {})
    assert info["open_now"] is False and info["next_opening"] == "tomorrow at 08:00"


def test_ai_pause_stops_all_automation_until_resumed(fashion, outbox, db):
    fashion.patch("/api/business/settings", json={"owner_notification_phone": OWNER})
    assert fashion.patch("/api/business/settings", json={"ai_enabled": False}).json()["ai_enabled"] is False
    runs = db.query(AgentRun).count()
    for t in ("black sneakers", "add 1", "hello?"):
        fashion.send(t)
    to_customer = [b for to, b in outbox.sent if to != OWNER]
    assert len(to_customer) == 1 and "Our team will reply here" in to_customer[0]  # one acknowledgement only
    assert db.query(AgentRun).count() == runs  # no AI at all
    conv = fashion.get("/api/conversations").json()[0]
    assert conv["needs_attention"] is True
    assert [n.kind for n in db.scalars(select(Notification))] == ["message_waiting"]
    detail = fashion.get(f"/api/conversations/{conv['id']}").json()
    assert [m["content"] for m in detail["messages"] if m["role"] == "customer"] == ["black sneakers", "add 1", "hello?"]
    fashion.patch("/api/business/settings", json={"ai_enabled": True})
    fashion.send("black sneakers")
    assert "Here's what I found" in outbox.sent[-1][1]


def test_ai_pause_blocks_order_confirmation(fashion, outbox, db):
    for m in ("black sneakers under 100k", "add 1", "deliver to Remera, KG 11 Ave"):
        fashion.send(m)
    fashion.patch("/api/business/settings", json={"ai_enabled": False})
    fashion.send("yes")
    assert fashion.get("/api/orders").json() == []
