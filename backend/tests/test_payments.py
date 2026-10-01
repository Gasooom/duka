import json
import uuid

import httpx
import pytest

from app.core.config import settings
from app.core.errors import ExternalServiceError
from app.integrations.payments import PaymentRequest, register_provider_override
from app.integrations.payments.momo import MoMoProvider
from tests.conftest import mock_signature


def _place_order(t, number="250788111222"):
    for m in ("black sneakers under 100k", "add 2", "place the order"):
        t.send(m, from_number=number)
    return t.get("/api/orders").json()[0]


def _callback(client, reference, status, sig=None):
    body = json.dumps({"reference": reference, "status": status}).encode()
    return client.post("/webhooks/payments/mock", content=body,
                       headers={"X-Mock-Signature": sig or mock_signature(body)})


def test_mock_payment_requires_confirmation(fashion, outbox, client, db):
    order = _place_order(fashion)
    fashion.send("pay")
    detail = fashion.get(f"/api/orders/{order['id']}").json()
    assert detail["status"] == "awaiting_payment"
    pay = detail["payments"][0]
    assert pay["status"] == "pending" and pay["provider"] == "mock"
    assert "approve" in outbox.sent[-1][1] and "PAID" not in outbox.sent[-1][1]

    # Paying again does not create a second charge
    fashion.send("pay")
    assert len(fashion.get(f"/api/orders/{order['id']}").json()["payments"]) == 1

    # Bad signature rejected, nothing changes
    assert _callback(client, pay["provider_reference"], "successful", sig="0" * 64).status_code == 401
    assert fashion.get(f"/api/orders/{order['id']}").json()["status"] == "awaiting_payment"

    r = _callback(client, pay["provider_reference"], "successful")
    assert r.json() == {"changed": True, "status": "successful"}
    detail = fashion.get(f"/api/orders/{order['id']}").json()
    assert detail["status"] == "paid" and detail["paid_at"]
    assert "Payment received" in outbox.sent[-1][1] and order["order_number"] in outbox.sent[-1][1]
    # Duplicate callback is idempotent: no second notification
    n = len(outbox.sent)
    assert _callback(client, pay["provider_reference"], "successful").json()["changed"] is False
    assert len(outbox.sent) == n


def test_failed_payment_keeps_order_unpaid_and_allows_retry(fashion, outbox, client):
    order = _place_order(fashion)
    fashion.send("pay")
    pay = fashion.get(f"/api/orders/{order['id']}").json()["payments"][0]
    _callback(client, pay["provider_reference"], "failed")
    detail = fashion.get(f"/api/orders/{order['id']}").json()
    assert detail["status"] == "awaiting_payment" and detail["payments"][0]["status"] == "failed"
    assert "not completed" in outbox.sent[-1][1]
    fashion.send("pay")
    assert len(fashion.get(f"/api/orders/{order['id']}").json()["payments"]) == 2


def test_dashboard_simulate_uses_same_workflow(fashion, outbox):
    order = _place_order(fashion)
    fashion.send("pay")
    pay = fashion.get(f"/api/orders/{order['id']}").json()["payments"][0]
    r = fashion.post(f"/api/payments/{pay['id']}/simulate", json={"status": "successful"})
    assert r.json()["status"] == "successful"
    assert fashion.get(f"/api/orders/{order['id']}").json()["status"] == "paid"


# ---------------------------------------------------------------- MTN MoMo (real client, mocked HTTP)
class FakeMoMo:
    def __init__(self, final_status="SUCCESSFUL"):
        self.calls = []
        self.final_status = final_status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path, dict(request.headers),
                           request.content.decode() if request.content else ""))
        if request.url.path == "/collection/token/":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if request.url.path == "/collection/v1_0/requesttopay" and request.method == "POST":
            return httpx.Response(202)
        if request.url.path.startswith("/collection/v1_0/requesttopay/"):
            return httpx.Response(200, json={"status": self.final_status, "financialTransactionId": "123"})
        return httpx.Response(404)


@pytest.fixture
def momo_env(monkeypatch):
    monkeypatch.setattr(settings, "momo_subscription_key", "sub")
    monkeypatch.setattr(settings, "momo_api_user", "user")
    monkeypatch.setattr(settings, "momo_api_key", "key")
    monkeypatch.setattr(settings, "momo_callback_host", "https://example.com")


def test_momo_provider_protocol(momo_env):
    fake = FakeMoMo()
    p = MoMoProvider(client=httpx.Client(transport=httpx.MockTransport(fake.handler), base_url="https://x"))
    res = p.request_payment(PaymentRequest(payment_id="pid-1", order_number="KF-1", amount=97000,
                                           currency="RWF", payer_phone="250788111222", description="test"))
    assert res.status == "pending"
    method, path, headers, body = fake.calls[1]
    assert path == "/collection/v1_0/requesttopay"
    assert headers["x-reference-id"] == res.reference
    assert headers["x-callback-url"] == "https://example.com/webhooks/payments/momo/pid-1"
    assert headers["authorization"] == "Bearer tok"
    assert json.loads(body)["payer"] == {"partyIdType": "MSISDN", "partyId": "250788111222"}
    assert p.get_status(res.reference).status == "successful"


def test_momo_not_configured_is_explicit():
    with pytest.raises(ExternalServiceError, match="BLOCKED BY EXTERNAL CREDENTIAL"):
        MoMoProvider()


def test_momo_callback_is_reverified_against_api(fashion, outbox, client, momo_env, db):
    fake = FakeMoMo(final_status="PENDING")
    provider = MoMoProvider(client=httpx.Client(transport=httpx.MockTransport(fake.handler)))
    register_provider_override("momo", provider)
    try:
        fashion.patch("/api/business/settings", json={"payment_provider": "momo"})
        order = _place_order(fashion)
        fashion.send("pay")
        pay = fashion.get(f"/api/orders/{order['id']}").json()["payments"][0]
        assert pay["provider"] == "momo"
        # Forged callback body claims success, but the MoMo API says PENDING -> nothing changes
        r = client.put(f"/webhooks/payments/momo/{pay['id']}", json={"status": "SUCCESSFUL"})
        assert r.json()["status"] == "pending"
        assert fashion.get(f"/api/orders/{order['id']}").json()["status"] == "awaiting_payment"
        fake.final_status = "SUCCESSFUL"
        assert client.put(f"/webhooks/payments/momo/{pay['id']}", json={}).json()["status"] == "successful"
        assert fashion.get(f"/api/orders/{order['id']}").json()["status"] == "paid"
        assert client.put(f"/webhooks/payments/momo/{uuid.uuid4()}", json={}).status_code == 404
    finally:
        register_provider_override("momo", None)
