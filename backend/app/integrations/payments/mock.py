"""Development payment provider.

It never auto-confirms. A payment stays 'pending' until a signed callback arrives at
POST /webhooks/payments/mock (or the dashboard's "simulate callback" admin action, which
goes through the same confirmation workflow). This lets the whole order flow be tested
without real money while preserving the 'confirmed-by-provider-only' rule."""
import uuid

from app.integrations.payments.base import PaymentProvider, PaymentRequest, ProviderResult


class MockPaymentProvider(PaymentProvider):
    name = "mock"
    verify_callbacks_via_status = False  # callbacks are HMAC-signed instead

    def request_payment(self, req: PaymentRequest) -> ProviderResult:
        ref = f"mock_{uuid.uuid4().hex[:16]}"
        return ProviderResult(reference=ref, status="pending",
                              raw={"note": "Awaiting simulated provider callback", "amount": str(req.amount)})

    def get_status(self, reference: str) -> ProviderResult:
        # Mock has no remote state; status only changes through the signed callback.
        return ProviderResult(reference=reference, status="pending")
