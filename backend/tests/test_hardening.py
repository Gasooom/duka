"""Production-mode guards and failure isolation."""
import json

import pytest

from app.core.config import settings


@pytest.fixture
def production(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "whatsapp_app_secret", "")


def test_dev_tools_disabled_in_production(fashion, production, client):
    assert fashion.post("/api/dev/simulate", json={"text": "hi"}).status_code == 404
    body = json.dumps({"reference": "x", "status": "successful"}).encode()
    assert client.post("/webhooks/payments/mock", content=body).status_code == 404


def test_unsigned_webhooks_rejected_in_production(fashion, production):
    assert fashion.send("hi").status_code == 503


def test_encryption_key_required_in_production(production):
    from app.core.security import encrypt_secret
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        encrypt_secret("token")


def test_one_bad_message_does_not_block_the_batch(fashion, outbox, monkeypatch):
    """A crash while processing one message rolls back that message only."""
    from app.workflows import inbound
    real = inbound.process_message
    calls = {"n": 0}

    def flaky(db, msg):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(db, msg)

    monkeypatch.setattr(inbound, "process_message", flaky)
    from app.integrations.whatsapp.parser import build_text_webhook
    p1 = build_text_webhook(fashion.phone_number_id, "1", "250788000001", "black sneakers", "w-a")
    p2 = build_text_webhook(fashion.phone_number_id, "1", "250788000002", "black sneakers", "w-b")
    p1["entry"][0]["changes"].append(p2["entry"][0]["changes"][0])
    results = inbound.process_webhook_payload(p1)
    assert [r.status for r in results] == ["error", "replied"]
    assert len(outbox.sent) == 1


def test_inbound_rate_limit_per_customer(fashion, outbox):
    from app.core.ratelimit import inbound_message_limiter
    inbound_message_limiter.limit = 3
    try:
        for i in range(5):
            fashion.send(f"jeans {i}")
        assert len(outbox.sent) == 3
    finally:
        inbound_message_limiter.limit = 30
