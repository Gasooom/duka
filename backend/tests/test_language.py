"""Conversation language: detection, persistence, switching, and every server-written message in the current
language — through the real pipeline (webhook -> inbox -> engine -> tools -> outbox)."""
import re
import string
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.agents.engine import build_system_prompt
from app.agents.language import SUPPORTED, detect, next_language
from app.i18n import MEDIA_LABELS, MESSAGES, t
from app.models import Business, Message
from app.services import hours
from tests.conftest import ADDRESS
from tests.test_ai_safety import call, model

NUMBER = "250788111222"
SAMBA = "Adidas Samba OG Black"


def conv(fashion):
    return fashion.get("/api/conversations").json()[0]


def lang_of(fashion):
    return conv(fashion)["language"]


def last_reply(outbox):
    return [b for to, b in outbox.sent if to == NUMBER][-1]


# ---------------------------------------------------------------- A–F: detection per language
@pytest.mark.parametrize("text,expected", [
    ("Hi, I'm looking for black sneakers under 100,000 RWF.", "en"),                      # A
    ("Can you tell me when my order will arrive?", "en"),
    ("Muraho, ndashaka inkweto z'umukara", "rw"),                                           # B
    ("Mufite telefone ya Samsung? Igiciro ni angahe?", "rw"),
    ("Bonjour, je cherche des baskets noires", "fr"),                                       # C
    ("Combien coûte la livraison à Remera ?", "fr"),
    ("Habari, nataka viatu vyeusi", "sw"),                                                  # D
    ("Bei gani ya simu hii? Asante", "sw"),
    ("السلام عليكم، أريد معرفة سعر هذا الهاتف", "ar"),                                      # E
    ("هل يتوفر باللون الأسود؟", "ar"),
    ("أريد طلب واحد", "ar"),
    ("السلام عليكم داير اعرف سعر التلفون ده", "ar-SD"),                                     # F
    ("عندكم اللون الاسود؟", "ar-SD"),
    ("عايز اطلب واحد", "ar-SD"),
    ("الطلب ده بوصل متين؟", "ar-SD"),
])
def test_first_message_sets_the_language(text, expected):
    assert next_language(None, detect(text))[0] == expected


def test_arabic_variant_is_decided_by_words_not_script():
    sd, msa = detect("الطلب ده بوصل متين؟"), detect("متى سيصل هذا الطلب؟")
    assert sd.code == "ar-SD" and msa.code == "ar"
    neutral = detect("اللون الاسود")  # Arabic with no dialect markers either way
    assert neutral.dialect_confidence == 0
    assert next_language("ar-SD", neutral)[0] == "ar-SD" and next_language("ar", neutral)[0] == "ar"


def test_weak_dialect_signal_does_not_flip_an_established_variant():
    weak = detect("عندكم اللون الاسود؟")
    assert weak.code == "ar-SD" and weak.dialect_confidence < 0.5
    assert next_language("ar", weak)[0] == "ar"  # not forced to Sudanese
    assert next_language("ar", detect("الطلب ده بوصل متين؟"))[0] == "ar-SD"  # a confident Sudanese message does


# ---------------------------------------------------------------- I, J, K: no switch on weak signals
@pytest.mark.parametrize("current,text", [
    ("en", "merci"), ("en", "شكرا"), ("rw", "hello"), ("rw", "ok"), ("ar-SD", "ok"), ("ar-SD", "Samba?"),
    ("fr", "yes"), ("sw", "👍"), ("ar-SD", "Thanks"),
])
def test_isolated_words_do_not_switch(current, text):
    assert next_language(current, detect(text))[0] == current


def test_product_names_say_nothing_about_language():
    names = {SAMBA.lower(), "sneakers", "black hoodie"}
    assert detect(SAMBA, names).code is None
    assert next_language("rw", detect("Adidas Samba OG Black", names))[0] == "rw"
    assert next_language("ar-SD", detect("داير Adidas Samba OG Black", names))[0] == "ar-SD"


@pytest.mark.parametrize("current,text", [
    ("rw", "Ndashaka black sneakers, size 42 please"),
    ("sw", "Nataka hizi sneakers, how much?"),
    ("fr", "Je veux le Black Hoodie, ok?"),
    ("ar-SD", "داير ال hoodie ده، how much?"),
    ("ar-SD", "Thanks شكرا, can you show me the hoodie please?"),
])
def test_code_switching_keeps_the_conversation_language(current, text):
    assert next_language(current, detect(text, {"sneakers", "black hoodie", "hoodie"}))[0] == current


# ---------------------------------------------------------------- G, H: persistence and switches in the pipeline
def test_sudanese_conversation_persists_then_switches_to_english_only_when_confident(fashion, outbox, db):
    for text in ("السلام عليكم داير جزمة سودا", "عندكم مقاس ٤٢؟", "ok", SAMBA, "Thanks"):
        fashion.send(text)
        assert lang_of(fashion) == "ar-SD", f"switched on {text!r}"
    assert "معليش" in last_reply(outbox) or "اللقيناهو" in last_reply(outbox)  # replies stay Sudanese
    fashion.send("Thanks, can you show me the black hoodie please?")
    c = conv(fashion)
    assert c["language"] == "en" and c["language_confidence"] >= 0.5
    assert last_reply(outbox).startswith("Here's what I found")
    # every inbound message keeps its own detection for debugging
    detections = [m.attributes["language"]["detected"] for m in db.scalars(
        select(Message).where(Message.role == "customer").order_by(Message.created_at))]
    assert detections[0] == "ar-SD" and detections[-1] == "en"


def test_english_conversation_switches_to_sudanese(fashion, outbox):
    fashion.send("Hi, I'm looking for black sneakers under 100k")
    assert lang_of(fashion) == "en"
    fashion.send("merci")
    assert lang_of(fashion) == "en"
    fashion.send("داير اعرف سعر التلفون ده")
    assert lang_of(fashion) == "ar-SD"
    reply = last_reply(outbox)  # the server's reply follows immediately
    assert reply.startswith(t("search_found", "ar-SD", count="").split("(")[0]) or reply == t("search_none", "ar-SD")


def test_no_signal_falls_back_to_the_business_language(fashion, outbox):
    fashion.patch("/api/business", json={"language": "fr"})
    fashion.send(SAMBA)
    assert lang_of(fashion) is None
    assert last_reply(outbox).startswith("Voici ce que j'ai trouvé")


# ---------------------------------------------------------------- 7/8: the model gets the language explicitly
def test_conversation_language_is_passed_to_the_model(fashion, outbox):
    scripted = model("أهلًا، داير شنو؟")
    fashion.send("السلام عليكم داير اعرف الاسعار")
    system = scripted.seen[0][0]["content"]
    assert "CONVERSATION LANGUAGE: Sudanese Arabic (ar-SD)" in system
    assert "do not switch to Modern Standard Arabic" in system
    assert "Never translate or change product names" in system


def test_prompt_language_rule_per_language():
    b = Business(name="X", business_type="retail", currency="RWF", delivery_enabled=True, payment_enabled=True,
                 human_handoff_enabled=True)
    from app.models import AgentConfig
    cfg = AgentConfig(tone="friendly", language="en")
    assert "Reply in Kinyarwanda" in build_system_prompt(b, cfg, "rw")
    assert "Reply in natural Arabic" in build_system_prompt(b, cfg, "ar")


# ---------------------------------------------------------------- L: server-written messages in the current language
def _sudanese_order(fashion, outbox):
    """Sudanese customer orders through the model; returns (summary text, confirmation text)."""
    model([call("search_products", query="adidas samba")], f"عندنا {SAMBA} بـ RWF 95,000.",
          [call("add_to_cart", product_ref="1")], "تمام، ضفناها.",
          [call("prepare_checkout", delivery_address=ADDRESS)], "ده الملخص.")
    fashion.send("السلام عليكم داير جزمة اديداس سامبا")
    fashion.send("عايز الاولى دي")
    fashion.send(f"وصلها لي في {ADDRESS}")
    summary = last_reply(outbox)
    fashion.send("ايوه")
    return summary, last_reply(outbox)


def test_order_summary_and_confirmation_in_sudanese(fashion, outbox):
    summary, confirmation = _sudanese_order(fashion, outbox)
    assert summary.startswith(t("summary_title", "ar-SD")) and t("confirm_prompt", "ar-SD") in summary
    assert f"1. {SAMBA} x1 @ RWF 95,000 = RWF 95,000" in summary
    assert "التوصيل (Kigali City): RWF 2,000" in summary and "الجملة: RWF 97,000" in summary
    assert f"التوصيل لـ: {ADDRESS}" in summary
    order = fashion.get("/api/orders").json()[0]
    assert confirmation.startswith(t("order_confirmed", "ar-SD", number=order["order_number"]))
    assert "الجملة: RWF 97,000" in confirmation and order["total"] == 97000
    assert lang_of(fashion) == "ar-SD"


def test_owner_status_updates_and_payments_follow_the_conversation_language(fashion, outbox):
    fashion.patch("/api/business/settings", json={"payment_instructions": "MoMo 0788 123 456 (Kigali Fashion)"})
    _sudanese_order(fashion, outbox)
    order = fashion.get("/api/orders").json()[0]
    fashion.patch(f"/api/orders/{order['id']}", json={"status": "accepted"})
    accepted = last_reply(outbox)
    assert accepted.startswith(t("accepted", "ar-SD", shop="Kigali Fashion", number=order["order_number"],
                                 total="RWF 97,000"))
    assert "MoMo 0788 123 456 (Kigali Fashion)" in accepted  # owner text kept exactly
    fashion.post(f"/api/orders/{order['id']}/payments", json={"method": "cash", "note": "Jane"})
    assert last_reply(outbox) == t("manual_payment_received", "ar-SD", number=order["order_number"],
                                   amount="RWF 97,000")
    fashion.patch(f"/api/orders/{order['id']}", json={"status": "out_for_delivery"})
    assert last_reply(outbox) == t("out_for_delivery", "ar-SD", number=order["order_number"])


@pytest.mark.parametrize("lang,text", [("rw", "Nshaka kuvugana n'umuntu"), ("ar-SD", "داير اتكلم مع زول"),
                                       ("fr", "Je veux parler à quelqu'un"), ("sw", "Nataka kuongea na mtu")])
def test_after_hours_handoff_in_the_customer_language(fashion, outbox, monkeypatch, lang, text):
    fashion.patch("/api/business", json={"business_hours": {"Mon-Sat": "08:00-20:00", "Sun": "closed"}})
    monkeypatch.setattr(hours, "_now", lambda: datetime(2026, 10, 10, 19, 0, tzinfo=timezone.utc))  # Sat 21:00
    fashion.send(text)
    opening = hours.next_opening({"Mon-Sat": "08:00-20:00", "Sun": "closed"}, "Africa/Kigali",
                                 datetime(2026, 10, 10, 19, 0, tzinfo=timezone.utc), lang)
    assert last_reply(outbox) == t("handoff_closed", lang, opening=opening)
    assert "08:00" in opening


def test_voice_note_and_ai_pause_replies_in_the_customer_language(fashion, outbox, client):
    from tests.test_whatsapp import _media
    fashion.send("Habari, nataka viatu vyeusi")
    fashion.patch("/api/business/settings", json={"ai_enabled": False})
    fashion.send("Habari, bado mpo?")
    assert last_reply(outbox) == t("ai_paused_soon", "sw")
    fashion.patch("/api/business/settings", json={"ai_enabled": True})
    fashion.post(f"/api/conversations/{conv(fashion)['id']}/handoff")
    fashion.post(f"/api/conversations/{conv(fashion)['id']}/return-to-ai")
    _media(fashion, client, "audio", "wvoice-sw")
    assert last_reply(outbox) == t("media_unsupported", "sw", label=MEDIA_LABELS["audio"]["sw"]) + t("handoff_message", "sw")


def test_arabic_yes_and_no_work_on_a_summary(fashion, outbox):
    model([call("search_products", query="adidas samba")], f"عندنا {SAMBA}.",
          [call("add_to_cart", product_ref="1")], "تمام.",
          [call("prepare_checkout", delivery_address=ADDRESS)], "-")
    for text in ("أريد حذاء اديداس سامبا", "أريد الأول", f"العنوان {ADDRESS}"):
        fashion.send(text)
    fashion.send("ايوه بس غير المقاس")  # "yes but change the size": not a confirmation
    assert fashion.get("/api/orders").json() == []
    fashion.send("نعم")
    assert len(fashion.get("/api/orders").json()) == 1


# ---------------------------------------------------------------- M: facts never change with the language
def _facts(text: str) -> dict:
    return {"money": sorted(re.findall(r"RWF [\d,]+", text)), "names": sorted(re.findall(r"Adidas Samba OG Black|"
            r"Classic Black T-Shirt", text)), "qty": sorted(re.findall(r"x\d+", text)), "address": ADDRESS in text}


def test_commerce_facts_are_identical_in_every_language(fashion, db):
    import uuid

    from app.models import Product
    from app.services.commerce_service import CartService, CheckoutService
    from app.services.conversation_service import ConversationService, CustomerService
    bid = uuid.UUID(fashion.business_id)
    customer = CustomerService(db, bid).upsert_from_whatsapp("250788123999")
    conversation = ConversationService(db, bid).get_or_create_active(customer)
    carts = CartService(db, bid)
    for sku, qty in (("KF-SAMBA-BLK", 2), ("KF-TEE-BLK", 3)):
        product = db.scalars(select(Product).where(Product.business_id == bid, Product.sku == sku)).one()
        carts.add_item(carts.get_active(customer, conversation), product, qty)
    checkout = CheckoutService(db, bid)
    summaries = {lang: checkout.prepare(customer, conversation, delivery_address=ADDRESS, language=lang)
                 for lang in SUPPORTED}
    english = _facts(summaries["en"].text)
    assert english["money"] == sorted(["RWF 95,000", "RWF 190,000", "RWF 12,000", "RWF 36,000", "RWF 226,000",
                                       "RWF 2,000", "RWF 228,000"])
    for lang, summary in summaries.items():
        assert _facts(summary.text) == english, lang
        assert summary.totals.total == summaries["en"].totals.total
    # the checkout fingerprint (what YES confirms) does not depend on the language
    assert len({str(s.totals.as_dict()) for s in summaries.values()}) == 1


def test_localised_tool_renders_keep_the_facts():
    from app.agents.render import render_tool_result
    result = {"ok": True, "count": 1, "products": [{"position": 1, "name": SAMBA, "price": 95000.0, "currency": "RWF",
                                                     "in_stock": True, "stock_quantity": 6}]}
    order = {"ok": True, "order_number": "KF-00012", "status": "accepted", "payment_status": "pending",
             "total": 97000.0, "currency": "RWF"}
    for lang in SUPPORTED:
        text = render_tool_result("search_products", {}, result, lang)
        assert f"1. {SAMBA} — RWF 95,000" in text
        status = render_tool_result("check_order_status", {}, order, lang)
        assert "KF-00012" in status and "RWF 97,000" in status


def test_every_message_exists_in_every_language_with_the_same_placeholders():
    for key, row in MESSAGES.items():
        assert set(row) == set(SUPPORTED), key
        fields = {lang: {f for _, f, _, _ in string.Formatter().parse(text) if f} for lang, text in row.items()}
        assert all(f == fields["en"] for f in fields.values()), (key, fields)


# ---------------------------------------------------------------- grounding still holds in Arabic
def test_invented_price_in_arabic_digits_is_caught(fashion, outbox, db):
    fashion.send("السلام عليكم داير جزمة")  # conversation becomes ar-SD
    model([call("search_products", query="adidas samba")], f"{SAMBA} سعرها ٨٠٬٠٠٠ RWF بس.")
    fashion.send("بكم الجزمة الاديداس سامبا؟")
    reply = last_reply(outbox)
    assert "٨٠٬٠٠٠" not in reply and "80,000" not in reply
    assert f"1. {SAMBA} — RWF 95,000 ({t('in_stock', 'ar-SD')})" in reply  # Sudanese server rendering


def test_correct_arabic_reply_passes_unchanged(fashion, outbox):
    fashion.send("السلام عليكم داير جزمة")
    text = f"عندنا {SAMBA} بـ RWF 95,000، داير تضيفها للسلة؟"
    model([call("search_products", query="adidas samba")], text)
    fashion.send("عندكم اديداس سامبا؟")
    assert last_reply(outbox) == text
