"""M5: an order exists only after the customer explicitly confirms a server-rendered summary; the owner is
notified and reviews it; a person can take over and the AI stays paused until explicitly returned."""
import uuid

import pytest
from sqlalchemy import select

from app.agents.intents import classify_confirmation, wants_human
from app.agents.providers import LLMResponse, ToolCall, set_provider_override
from app.i18n import t
from app.integrations.whatsapp.adapters import SendResult, set_adapter_override
from app.models import AgentRun, Notification, Order
from tests.conftest import ADDRESS, CapturingAdapter, place_order
from tests.test_agent import Scripted

NUMBER = "250788111222"
OWNER = "250788000999"


def _orders(db):
    db.expire_all()
    return list(db.scalars(select(Order)))


def _to_customer(outbox, number=NUMBER):
    return [body for to, body in outbox.sent if to == number]


def _shop_until_summary(t, number=NUMBER):
    for m in ("black sneakers under 100k", "add 1", f"deliver to {ADDRESS}"):
        t.send(m, from_number=number)


# ---------------------------------------------------------------- explicit confirmation
@pytest.mark.parametrize("text,expected", [
    ("yes", "yes"), ("YES!", "yes"), ("Yes please", "yes"), ("ok", "yes"), ("confirm", "yes"), ("👍", "yes"),
    ("Yego", "yes"), ("yego rwose", "yes"), ("Ndabyemeje", "yes"), ("Oui", "yes"), ("oui merci", "yes"),
    ("D'accord", "yes"), ("je confirme", "yes"), ("Ndiyo", "yes"), ("sawa", "yes"),
    ("no", "no"), ("No thanks", "no"), ("cancel", "no"), ("Oya", "no"), ("non", "no"), ("hapana", "no"),
    ("yes but change the size", None), ("ok maybe later", None), ("sounds good, add socks too", None),
    ("what is the delivery fee?", None), ("yesterday I ordered", None), ("", None), ("okay okay okay okay okay okay okay", None),
])
def test_confirmation_is_strict_and_multilingual(text, expected):
    assert classify_confirmation(text) == expected


def test_summary_is_not_an_order_and_yes_places_exactly_one(fashion, outbox, db):
    _shop_until_summary(fashion)
    assert "Order summary" in outbox.sent[-1][1] and _orders(db) == []
    fashion.send("yes")
    assert len(_orders(db)) == 1 and "confirmed" in outbox.sent[-1][1]
    fashion.send("yes")  # a second YES does not create a second order
    assert len(_orders(db)) == 1


@pytest.mark.parametrize("word", ["Yego", "Oui", "👍"])
def test_confirmation_in_other_languages(fashion, outbox, db, word):
    _shop_until_summary(fashion)
    fashion.send(word)
    assert len(_orders(db)) == 1


def test_ambiguous_reply_does_not_place_an_order(fashion, outbox, db):
    _shop_until_summary(fashion)
    fashion.send("yes but can I change the size?")
    assert _orders(db) == []


def test_no_declines_and_a_later_yes_does_nothing(fashion, outbox, db):
    _shop_until_summary(fashion)
    fashion.send("no")
    assert "not placed" in outbox.sent[-1][1]
    fashion.send("yes")
    assert _orders(db) == []


def test_cart_change_after_summary_requires_a_new_confirmation(fashion, outbox, db):
    _shop_until_summary(fashion)
    fashion.send("classic black t-shirt")
    fashion.send("add 1")
    fashion.send("yes")
    assert _orders(db) == [] and "updated summary" in outbox.sent[-1][1] and "T-Shirt" in outbox.sent[-1][1]
    fashion.send("yes")
    [order] = _orders(db)
    assert len(order.items) == 2


def test_summary_that_never_reached_the_customer_cannot_be_confirmed(fashion, db):
    class FailsOnSummary(CapturingAdapter):
        def send_text(self, to, body):
            if "Order summary" in body:
                return SendResult(ok=False, wa_message_id=None, delivery_status="failed", error="HTTP 400")
            return super().send_text(to, body)

    set_adapter_override(FailsOnSummary())
    _shop_until_summary(fashion)
    fashion.send("yes")
    assert _orders(db) == []


def test_misbehaving_llm_cannot_place_an_order_or_change_the_summary(fashion, outbox, db):
    """The model tries every shortcut: a create_order tool, claiming the order is placed, inventing a total."""
    fashion.send("black sneakers under 100k")
    fashion.send("add 1")
    scripted = Scripted([
        LLMResponse(content=None, tool_calls=[ToolCall("1", "create_order", {}),
                                              ToolCall("2", "prepare_checkout", {"delivery_address": ADDRESS})]),
        LLMResponse(content="Done! Your order KF-00001 is placed and paid. Total RWF 1."),
    ])
    set_provider_override(scripted)
    fashion.send("yes order it now, deliver to Remera, KG 11 Ave")
    reply = outbox.sent[-1][1]
    assert _orders(db) == [], "the YES that came with the request is not a confirmation of a summary"
    assert "Order summary" in reply and "RWF 1." not in reply and "placed and paid" not in reply
    run = db.scalars(select(AgentRun).order_by(AgentRun.created_at.desc())).first()
    create = next(s for s in run.steps if s.get("tool") == "create_order")
    assert create["ok"] is False and "Unknown" in create["error"]
    seen = len(scripted.seen)
    fashion.send("yes")
    assert len(_orders(db)) == 1 and len(scripted.seen) == seen  # confirmed without asking the LLM


# ---------------------------------------------------------------- owner notification + review
def test_owner_is_notified_and_reviews_the_order(fashion, outbox, db):
    fashion.patch("/api/business/settings", json={"owner_notification_phone": OWNER,
                                                  "payment_instructions": "MoMo 0788 123 456"})
    order = place_order(fashion)
    owner = _to_customer(outbox, OWNER)
    assert len(owner) == 1 and order["order_number"] in owner[0] and ADDRESS in owner[0]
    r = fashion.patch(f"/api/orders/{order['id']}", json={"status": "accepted"}).json()
    assert r["status"] == "accepted" and "MoMo 0788 123 456" in _to_customer(outbox)[-1]


def test_owner_rejection_restocks_and_tells_the_customer(fashion, outbox):
    order = place_order(fashion)
    pid = order["items"][0]["product_id"]
    stock = fashion.get(f"/api/products/{pid}").json()["stock_quantity"]
    r = fashion.patch(f"/api/orders/{order['id']}", json={"status": "cancelled", "reason": "Size 42 is sold out"})
    assert r.json()["status"] == "cancelled" and r.json()["cancel_reason"] == "Size 42 is sold out"
    assert "cancelled: Size 42 is sold out" in _to_customer(outbox)[-1]
    assert fashion.get(f"/api/products/{pid}").json()["stock_quantity"] == stock + 1


def test_notification_without_phone_is_recorded_not_sent(fashion, outbox, db):
    place_order(fashion)
    [n] = db.scalars(select(Notification)).all()
    assert n.kind == "new_order" and n.status == "skipped"
    assert _to_customer(outbox, OWNER) == []


def test_owner_notification_uses_template_when_configured(fashion, db):
    class TemplateCapture(CapturingAdapter):
        templates: list = []

        def send_template(self, to, name, language, params):
            self.templates.append((to, name, language, params))
            return SendResult(ok=True, wa_message_id="wamid.tpl", delivery_status="sent")

    adapter = TemplateCapture()
    set_adapter_override(adapter)
    fashion.patch("/api/business/settings", json={"owner_notification_phone": OWNER,
                                                  "owner_notification_template": "new_order_alert"})
    order = place_order(fashion)
    [(to, name, lang, params)] = adapter.templates
    assert (to, name, lang) == (OWNER, "new_order_alert", "en") and order["order_number"] in params[0]


# ---------------------------------------------------------------- human control
@pytest.mark.parametrize("text,lang", [("I want to talk to a person", "en"),
                                       ("Can I speak with someone from the shop?", "en"),
                                       ("Nshaka kuvugana n'umuntu", "rw"), ("Je veux parler à quelqu'un", "fr"),
                                       ("Nataka kuongea na mtu", "sw"), ("داير اتكلم مع زول", "ar-SD"),
                                       ("أريد التحدث مع موظف", "ar")])
def test_customer_can_ask_for_a_person_in_any_supported_language(fashion, outbox, db, text, lang):
    assert wants_human(text)
    fashion.patch("/api/business/settings", json={"owner_notification_phone": OWNER})
    scripted = Scripted([])  # the LLM must not even be called
    set_provider_override(scripted)
    fashion.send(text)
    conv = fashion.get("/api/conversations").json()[0]
    assert conv["status"] == "human" and conv["needs_attention"] is True and conv["language"] == lang
    assert _to_customer(outbox)[-1] == t("handoff", lang)  # answered in the customer's language
    assert any("needs a person" in b for b in _to_customer(outbox, OWNER))
    assert scripted.seen == []


def test_ordinary_messages_are_not_mistaken_for_a_handoff():
    for text in ("black sneakers under 100k", "how much is delivery to Remera?", "do you have a person-sized bag",
                 "human hair wigs", "I'll pay now", "inkweto z'umukara", "inkweto z'umuntu mukuru",
                 "une robe pour une personne", "nguo za mtu mzima", "the owner manual for this phone"):
        assert not wants_human(text), text


def test_handoff_disabled_gives_contact_instead(fashion, outbox):
    fashion.patch("/api/business", json={"human_handoff_enabled": False, "phone": "+250788123456"})
    fashion.send("I want to talk to a person")
    assert fashion.get("/api/conversations").json()[0]["status"] == "ai"
    assert "+250788123456" in outbox.sent[-1][1]


def test_takeover_pauses_ai_until_explicitly_returned(fashion, outbox, db):
    fashion.send("black sneakers")
    conv = fashion.get("/api/conversations").json()[0]
    runs_before = db.query(AgentRun).count()
    assert fashion.post(f"/api/conversations/{conv['id']}/handoff").json()["status"] == "human"
    n = len(outbox.sent)
    for text in ("hello?", "add 1", "yes", "I want a refund"):
        fashion.send(text)
    assert len(outbox.sent) == n and db.query(AgentRun).count() == runs_before  # AI fully paused
    detail = fashion.get(f"/api/conversations/{conv['id']}").json()
    assert [m["content"] for m in detail["messages"] if m["role"] == "customer"][-4:] == \
        ["hello?", "add 1", "yes", "I want a refund"]  # visible to the owner
    assert fashion.get("/api/conversations").json()[0]["needs_attention"] is True
    r = fashion.post(f"/api/conversations/{conv['id']}/reply", json={"text": "Hi, Jane here. Refund approved."})
    assert r.json()["delivery_status"] == "sent" and outbox.sent[-1][1] == "Hi, Jane here. Refund approved."
    r = fashion.post(f"/api/conversations/{conv['id']}/return-to-ai",
                     json={"message": "You're back with our assistant. Ask me anything!"})
    assert r.json()["status"] == "ai" and outbox.sent[-1][1].startswith("You're back")
    assert fashion.post(f"/api/conversations/{conv['id']}/return-to-ai").status_code == 400
    fashion.send("black sneakers")
    assert db.query(AgentRun).count() == runs_before + 1
    actions = [e["action"] for e in fashion.get("/api/dashboard/audit").json()]
    assert {"conversation.taken_over", "conversation.returned_to_ai"} <= set(actions)


def test_handoff_discards_a_pending_summary(fashion, outbox, db):
    """A person may have changed the deal: after a handoff the customer must confirm a fresh summary."""
    _shop_until_summary(fashion)
    conv = fashion.get("/api/conversations").json()[0]
    fashion.post(f"/api/conversations/{conv['id']}/handoff")
    fashion.post(f"/api/conversations/{conv['id']}/return-to-ai")
    fashion.send("yes")
    assert _orders(db) == []


def test_staff_reply_requires_takeover(fashion, outbox):
    fashion.send("black sneakers")
    conv = fashion.get("/api/conversations").json()[0]
    assert fashion.post(f"/api/conversations/{conv['id']}/reply", json={"text": "hi"}).status_code == 400


def test_order_status_wording_never_claims_payment(fashion, outbox):
    order = place_order(fashion)
    fashion.send(f"status of order {order['order_number']}")
    reply = outbox.sent[-1][1]
    assert "waiting for the shop's review" in reply and "Payment: unpaid" in reply
    assert uuid.UUID(order["id"])
