import json

import httpx

from app.core.config import settings
from app.core.security import decrypt_secret
from app.integrations.whatsapp.adapters import CloudWhatsAppAdapter
from app.integrations.whatsapp.parser import parse_webhook
from app.models import Message, WhatsAppAccount
from tests.conftest import drain


def test_webhook_verification(client):
    ok = client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "test-verify-token",
                                                  "hub.challenge": "98765"})
    assert ok.status_code == 200 and ok.text == "98765"
    bad = client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                                   "hub.challenge": "1"})
    assert bad.status_code == 403


def test_signature_verification(fashion, outbox, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_app_secret", "app-secret")
    assert fashion.send("hi").status_code == 401
    assert fashion.send("hi", sign_secret="wrong").status_code == 401
    assert fashion.send("hi", sign_secret="app-secret").status_code == 200
    assert len(outbox.sent) == 1


def test_parser_handles_interactive_and_statuses():
    payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "123"},
        "contacts": [{"wa_id": "250788", "profile": {"name": "Ann"}}],
        "messages": [{"from": "250788", "id": "w1", "type": "interactive",
                      "interactive": {"type": "button_reply", "button_reply": {"id": "b1", "title": "Pay now"}}},
                     {"from": "250788", "id": "w2", "type": "image", "image": {"id": "img"}}],
        "statuses": [{"id": "out1", "status": "delivered", "recipient_id": "250788"}]}}]}]}
    msgs, statuses = parse_webhook(payload)
    assert msgs[0].text == "Pay now" and msgs[0].profile_name == "Ann"
    assert msgs[1].type == "image" and msgs[1].text is None
    assert statuses[0].status == "delivered"
    assert parse_webhook({"object": "page"}) == ([], [])


def test_duplicate_webhook_delivery_is_processed_once(fashion, outbox, db):
    fashion.send("black sneakers", wa_id="wamid.DUP1")
    fashion.send("black sneakers", wa_id="wamid.DUP1")  # Meta retry
    assert len(outbox.sent) == 1
    assert db.query(Message).filter_by(role="customer").count() == 1


def test_unknown_phone_number_id_is_dropped(client, outbox):
    from app.integrations.whatsapp.parser import build_text_webhook
    r = client.post("/webhooks/whatsapp", json=build_text_webhook("unknown-pnid", "1", "250788", "hi", "w1"))
    assert r.status_code == 200 and outbox.sent == []


def _media(fashion, client, mtype, wa_id):
    payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": fashion.phone_number_id},
        "messages": [{"from": "250788111222", "id": wa_id, "type": mtype, mtype: {"id": "x"}}]}}]}]}
    client.post("/webhooks/whatsapp", json=payload)
    drain()


def test_voice_notes_and_media_go_to_a_human_not_a_guess(fashion, outbox, client):
    _media(fashion, client, "audio", "wvoice")
    assert "can't open voice notes" in outbox.sent[0][1] and "passed your message to our team" in outbox.sent[0][1]
    conv = fashion.get("/api/conversations").json()[0]
    assert conv["status"] == "human" and conv["needs_attention"] is True
    assert "voice notes" in conv["handoff_reason"]
    _media(fashion, client, "image", "wimg")  # AI stays silent while a person handles it
    assert len(outbox.sent) == 1


def test_media_without_handoff_gets_text_only_reply_and_reactions_are_ignored(fashion, outbox, client):
    fashion.patch("/api/business", json={"human_handoff_enabled": False})
    _media(fashion, client, "reaction", "wreact")
    assert outbox.sent == []
    _media(fashion, client, "image", "wimg")
    assert "only read text" in outbox.sent[0][1]


def test_human_handoff_stops_ai(fashion, outbox):
    fashion.send("I want to talk to a human please")
    conv = fashion.get("/api/conversations").json()[0]
    assert conv["status"] == "human" and conv["needs_attention"] is True
    n = len(outbox.sent)
    fashion.send("hello? anyone?")
    assert len(outbox.sent) == n  # AI stays silent
    assert fashion.get("/api/conversations", params={"needs_attention": True}).json()
    # staff replies, then returns to AI
    assert fashion.post(f"/api/conversations/{conv['id']}/reply", json={"text": "Hi, this is Jane."}).status_code == 200
    assert outbox.sent[-1][1] == "Hi, this is Jane."
    fashion.post(f"/api/conversations/{conv['id']}/return-to-ai")
    fashion.send("black sneakers")
    assert len(outbox.sent) == n + 2


def test_handoff_tool_disabled_by_config(fashion, outbox):
    fashion.patch("/api/business", json={"human_handoff_enabled": False})
    fashion.send("I want to talk to a human")
    assert fashion.get("/api/conversations").json()[0]["status"] == "ai"


def test_access_token_encrypted_at_rest(fashion, db):
    r = fashion.post("/api/whatsapp/accounts", json={"phone_number_id": "pnid-cloud", "mode": "cloud",
                                                      "access_token": "EAAG-secret-token"})
    assert r.status_code == 201 and r.json()["has_access_token"] is True and "access_token" not in r.json()
    acct = db.query(WhatsAppAccount).filter_by(phone_number_id="pnid-cloud").one()
    assert "EAAG" not in acct.access_token_encrypted
    assert decrypt_secret(acct.access_token_encrypted) == "EAAG-secret-token"
    assert fashion.post("/api/whatsapp/accounts", json={"phone_number_id": "p2", "mode": "cloud"}).status_code == 422


def test_cloud_adapter_request_shape_and_retries(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"messages": [{"id": "wamid.OK"}]})

    a = CloudWhatsAppAdapter("PNID", "TOKEN", client=httpx.Client(transport=httpx.MockTransport(handler)))
    res = a.send_text("250788111222", "hello")
    assert res.ok and res.wa_message_id == "wamid.OK" and len(calls) == 2
    req = calls[-1]
    assert req.url.path.endswith("/PNID/messages") and req.headers["authorization"] == "Bearer TOKEN"
    assert json.loads(req.content) == {"messaging_product": "whatsapp", "recipient_type": "individual",
                                       "to": "250788111222", "type": "text",
                                       "text": {"preview_url": False, "body": "hello"}}


def test_cloud_adapter_does_not_retry_permanent_errors():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(401, json={"error": {"message": "Invalid OAuth access token"}})

    res = CloudWhatsAppAdapter("P", "T", client=httpx.Client(transport=httpx.MockTransport(handler))).send_text("1", "x")
    assert not res.ok and res.delivery_status == "failed" and len(calls) == 1 and "401" in res.error


def test_send_failure_is_recorded_not_raised(fashion, db, monkeypatch):
    """Cloud account whose token is missing: message stored as failed, webhook still 200."""
    acct = db.query(WhatsAppAccount).filter_by(phone_number_id=fashion.phone_number_id).one()
    acct.mode = "cloud"
    db.commit()
    assert fashion.send("black sneakers").status_code == 200
    out = db.query(Message).filter_by(role="assistant").one()
    assert out.delivery_status == "failed" and "token" in out.attributes["error"]


def test_conversation_messages_are_strictly_ordered(fashion, outbox):
    """Regression: messages written in one transaction must not share a timestamp (now() vs clock_timestamp())."""
    for t in ("black sneakers under 100k", "add 2", "show my cart"):
        fashion.send(t)
    conv = fashion.get("/api/conversations").json()[0]
    msgs = [m for m in fashion.get(f"/api/conversations/{conv['id']}").json()["messages"]
            if m["role"] in ("customer", "assistant")]
    assert [m["role"] for m in msgs] == ["customer", "assistant"] * 3
    stamps = [m["created_at"] for m in msgs]
    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)
