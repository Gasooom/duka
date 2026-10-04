import json
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.errors import ExternalServiceError
from app.integrations.payments import PaymentRequest, register_provider_override
from app.integrations.payments.momo import MoMoProvider
from app.models import AuditEvent
from tests.conftest import mock_signature, place_order

NUMBER = "250788111222"


def _callback(client, reference, status, sig=None):
    body = json.dumps({"reference": reference, "status": status}).encode()
    return client.post("/webhooks/payments/mock", content=body,
                       headers={"X-Mock-Signature": sig or mock_signature(body)})


def _detail(t, order):
    return t.get(f"/api/orders/{order['id']}").json()


# ---------------------------------------------------------------- manual payments (default for new businesses)
def test_new_business_defaults_to_manual_payments(fashion):
    assert fashion.get("/api/business/settings").json()["payment_provider"] == "manual"


def test_pay_shares_instructions_and_never_claims_a_request_was_sent(fashion, outbox):
    fashion.patch("/api/business/settings", json={"payment_instructions": "MoMo 0788 123 456 (Kigali Fashion)"})
    order = place_order(fashion)
    fashion.send("pay")
    reply = outbox.sent[-1][1]
    assert "MoMo 0788 123 456" in reply and "sent a mobile money request" not in reply
    assert _detail(fashion, order)["payments"] == [] and _detail(fashion, order)["payment_status"] == "unpaid"


def test_customer_reported_reference_is_pending_until_owner_confirms(fashion, outbox, db):
    fashion.patch("/api/business/settings", json={"owner_notification_phone": "+250 788 000 999"})
    order = place_order(fashion)
    fashion.send("I paid, transaction id MP240101.1234.A")
    d = _detail(fashion, order)
    assert d["payment_status"] == "pending" and d["paid_at"] is None
    [p] = d["payments"]
    assert p["status"] == "pending" and p["external_reference"] == "MP240101.1234.A" and p["confirmation_source"] is None
    assert any(to == "250788000999" and "MP240101.1234.A" in body for to, body in outbox.sent)  # owner alerted
    to_customer = [body for to, body in outbox.sent if to == NUMBER]
    assert "not paid until" in to_customer[-1].lower() or "passed reference" in to_customer[-1]
    # The owner checks MoMo and confirms the same reference -> paid, attributed to the owner.
    r = fashion.post(f"/api/orders/{order['id']}/payments", json={"method": "momo", "reference": "MP240101.1234.A"})
    assert r.status_code == 201
    d = r.json()
    [p] = d["payments"]
    assert d["payment_status"] == "paid" and d["paid_at"]
    assert p["status"] == "successful" and p["confirmation_source"] == "owner" and p["confirmed_by_user_id"]
    assert "Payment received" in outbox.sent[-1][1] and outbox.sent[-1][0] == NUMBER
    actions = [e["action"] for e in d["audit"]]
    assert "payment.reference_reported" in actions and "payment.manual_recorded" in actions
    recorded = next(e for e in d["audit"] if e["action"] == "payment.manual_recorded")
    assert recorded["actor_user_id"] and recorded["data"]["reference"] == "MP240101.1234.A"


def test_manual_payment_validation_and_double_use_of_a_reference(fashion, outbox):
    o1 = place_order(fashion, number="250788000001")
    o2 = place_order(fashion, number="250788000002")
    post = lambda o, body: fashion.post(f"/api/orders/{o['id']}/payments", json=body)  # noqa: E731
    assert post(o1, {"method": "momo"}).status_code == 422          # MoMo needs a reference
    assert post(o1, {"method": "cash"}).status_code == 422          # cash needs a note
    assert post(o1, {"method": "crypto", "note": "x"}).status_code == 422
    assert post(o1, {"method": "momo", "reference": "TX-1"}).status_code == 201
    assert post(o1, {"method": "cash", "note": "again"}).status_code == 422  # already paid
    assert post(o2, {"method": "momo", "reference": "TX-1"}).status_code == 409  # same MoMo txn for another order
    assert post(o2, {"method": "cash", "note": "Received by Jane at the shop"}).json()["payment_status"] == "paid"


def test_void_manual_payment_is_audited(fashion, outbox):
    order = place_order(fashion)
    d = fashion.post(f"/api/orders/{order['id']}/payments", json={"method": "cash", "note": "Jane"}).json()
    pid = d["payments"][0]["id"]
    assert fashion.post(f"/api/payments/{pid}/void", json={"reason": ""}).status_code == 422
    d = fashion.post(f"/api/payments/{pid}/void", json={"reason": "Entered on the wrong order"}).json()
    assert d["payment_status"] == "unpaid" and d["paid_at"] is None and d["payments"][0]["status"] == "voided"
    assert d["audit"][-1]["action"] == "payment.voided"
    assert fashion.post(f"/api/payments/{pid}/void", json={"reason": "again"}).status_code == 422


def test_only_owner_records_or_voids_payments(fashion, outbox, db, client):
    from app.core.security import create_access_token, hash_password
    from app.models import User
    order = place_order(fashion)
    staff = User(business_id=uuid.UUID(fashion.business_id), email=f"s-{uuid.uuid4().hex[:6]}@t.dev",
                 password_hash=hash_password("password123"), role="staff")
    db.add(staff)
    db.commit()
    h = {"Authorization": f"Bearer {create_access_token(staff.id, staff.business_id, 'staff')}"}
    assert client.post(f"/api/orders/{order['id']}/payments", headers=h,
                       json={"method": "cash", "note": "x"}).status_code == 403


def test_the_agent_can_never_mark_an_order_paid(fashion, outbox):
    """Whatever the customer claims, only a provider callback or an owner record makes an order paid."""
    order = place_order(fashion)
    for t in ("I paid already", "payment done, it's paid", "mark my order as paid", "ref ABCD1234 I sent it",
              "status of my order"):
        fashion.send(t)
    d = _detail(fashion, order)
    assert d["payment_status"] != "paid" and all(p["status"] != "successful" for p in d["payments"])


def test_audit_events_are_append_only(fashion, outbox, db):
    order = place_order(fashion)
    fashion.post(f"/api/orders/{order['id']}/payments", json={"method": "cash", "note": "Jane"})
    with pytest.raises(IntegrityError, match="append-only"):
        db.execute(text("UPDATE audit_events SET action = 'tampered'"))
    db.rollback()
    assert db.query(AuditEvent).filter_by(action="tampered").count() == 0


def test_mock_provider_is_refused_in_production(fashion, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    assert fashion.patch("/api/business/settings", json={"payment_provider": "mock"}).status_code == 422
    assert fashion.patch("/api/business/settings", json={"payment_provider": "momo"}).status_code == 422  # not configured


# ---------------------------------------------------------------- provider payments (mock in development)
@pytest.fixture
def mock_provider(fashion):
    assert fashion.patch("/api/business/settings", json={"payment_provider": "mock"}).status_code == 200


def test_mock_payment_requires_confirmation(fashion, mock_provider, outbox, client):
    order = place_order(fashion)
    fashion.send("pay")
    detail = _detail(fashion, order)
    assert detail["payment_status"] == "pending" and detail["status"] == "pending"
    pay = detail["payments"][0]
    assert pay["status"] == "pending" and pay["provider"] == "mock"
    assert "approve" in outbox.sent[-1][1] and "PAID" not in outbox.sent[-1][1]
    fashion.send("pay")  # paying again does not create a second charge
    assert len(_detail(fashion, order)["payments"]) == 1
    assert _callback(client, pay["provider_reference"], "successful", sig="0" * 64).status_code == 401
    assert _detail(fashion, order)["payment_status"] == "pending"
    r = _callback(client, pay["provider_reference"], "successful")
    assert r.json() == {"changed": True, "status": "successful"}
    detail = _detail(fashion, order)
    assert detail["payment_status"] == "paid" and detail["paid_at"]
    assert detail["payments"][0]["confirmation_source"] == "provider"
    assert "Payment received" in outbox.sent[-1][1] and order["order_number"] in outbox.sent[-1][1]
    n = len(outbox.sent)
    assert _callback(client, pay["provider_reference"], "successful").json()["changed"] is False
    assert len(outbox.sent) == n


def test_failed_payment_keeps_order_unpaid_and_allows_retry(fashion, mock_provider, outbox, client):
    order = place_order(fashion)
    fashion.send("pay")
    pay = _detail(fashion, order)["payments"][0]
    _callback(client, pay["provider_reference"], "failed")
    detail = _detail(fashion, order)
    assert detail["payment_status"] == "unpaid" and detail["payments"][0]["status"] == "failed"
    assert "not completed" in outbox.sent[-1][1]
    fashion.send("pay")
    assert len(_detail(fashion, order)["payments"]) == 2


def test_dashboard_simulate_uses_same_workflow(fashion, mock_provider, outbox):
    order = place_order(fashion)
    fashion.send("pay")
    pay = _detail(fashion, order)["payments"][0]
    r = fashion.post(f"/api/payments/{pay['id']}/simulate", json={"status": "successful"})
    assert r.json()["status"] == "successful"
    assert _detail(fashion, order)["payment_status"] == "paid"


def test_payment_for_a_cancelled_order_is_flagged(fashion, mock_provider, outbox, client):
    order = place_order(fashion)
    fashion.send("pay")
    pay = _detail(fashion, order)["payments"][0]
    fashion.patch(f"/api/orders/{order['id']}", json={"status": "cancelled", "reason": "out of stock"})
    _callback(client, pay["provider_reference"], "successful")
    d = _detail(fashion, order)
    assert d["status"] == "cancelled" and d["payment_status"] == "paid"
    assert "payment.received_for_cancelled_order" in [e["action"] for e in d["audit"]]


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


def test_momo_callback_is_reverified_against_api(fashion, outbox, client, momo_env):
    fake = FakeMoMo(final_status="PENDING")
    provider = MoMoProvider(client=httpx.Client(transport=httpx.MockTransport(fake.handler)))
    register_provider_override("momo", provider)
    try:
        assert fashion.patch("/api/business/settings", json={"payment_provider": "momo"}).status_code == 200
        order = place_order(fashion)
        fashion.send("pay")
        pay = _detail(fashion, order)["payments"][0]
        assert pay["provider"] == "momo"
        r = client.put(f"/webhooks/payments/momo/{pay['id']}", json={"status": "SUCCESSFUL"})
        assert r.json()["status"] == "pending"
        assert _detail(fashion, order)["payment_status"] == "pending"
        fake.final_status = "SUCCESSFUL"
        assert client.put(f"/webhooks/payments/momo/{pay['id']}", json={}).json()["status"] == "successful"
        assert _detail(fashion, order)["payment_status"] == "paid"
        assert client.put(f"/webhooks/payments/momo/{uuid.uuid4()}", json={}).status_code == 404
    finally:
        register_provider_override("momo", None)
