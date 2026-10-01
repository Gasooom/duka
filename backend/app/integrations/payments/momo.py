"""MTN Mobile Money (MoMo) Collection API — Request to Pay.

Docs: https://momodeveloper.mtn.com/api-documentation
Flow:
  1. POST /collection/token/            (Basic api_user:api_key + subscription key) -> access token
  2. POST /collection/v1_0/requesttopay (X-Reference-Id = our UUID, X-Callback-Url) -> 202 Accepted
  3. Customer approves on their phone.
  4. MoMo PUTs to the callback URL. Callbacks are NOT signed, so we never trust the body:
     we re-query GET /collection/v1_0/requesttopay/{referenceId} before updating anything.

Requires MOMO_SUBSCRIPTION_KEY, MOMO_API_USER, MOMO_API_KEY (BLOCKED BY EXTERNAL CREDENTIAL
until provided). Sandbox only accepts currency EUR -> set MOMO_CURRENCY_OVERRIDE=EUR there.
"""
import time
import uuid

import httpx

from app.core.config import settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger, log_event
from app.integrations.payments.base import PaymentProvider, PaymentRequest, ProviderResult

logger = get_logger(__name__)
_STATUS_MAP = {"SUCCESSFUL": "successful", "FAILED": "failed", "REJECTED": "failed", "TIMEOUT": "failed",
               "PENDING": "pending"}


class MoMoProvider(PaymentProvider):
    name = "momo"
    verify_callbacks_via_status = True

    def __init__(self, client: httpx.Client | None = None):
        missing = [k for k in ("momo_subscription_key", "momo_api_user", "momo_api_key") if not getattr(settings, k)]
        if missing:
            raise ExternalServiceError(
                f"MoMo not configured: set {', '.join(m.upper() for m in missing)} (BLOCKED BY EXTERNAL CREDENTIAL)",
                code="payment_provider_not_configured")
        self.base = settings.momo_base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=15)
        self._token: tuple[str, float] | None = None

    def _headers(self) -> dict[str, str]:
        return {"Ocp-Apim-Subscription-Key": settings.momo_subscription_key,
                "X-Target-Environment": settings.momo_target_environment}

    def _access_token(self) -> str:
        if self._token and self._token[1] > time.time() + 30:
            return self._token[0]
        r = self._request("POST", "/collection/token/", auth=(settings.momo_api_user, settings.momo_api_key),
                          headers={"Ocp-Apim-Subscription-Key": settings.momo_subscription_key})
        data = r.json()
        self._token = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
        return self._token[0]

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = self.client.request(method, self.base + path, **kw)
                if r.status_code in (429, 500, 502, 503, 504):
                    last = ExternalServiceError(f"MoMo HTTP {r.status_code}")
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                if r.status_code >= 400:
                    raise ExternalServiceError(f"MoMo HTTP {r.status_code}: {r.text[:300]}")
                return r
            except httpx.TransportError as exc:
                last = exc
                time.sleep(0.5 * (2 ** attempt))
        raise ExternalServiceError(f"MoMo request failed after retries: {last}")

    def request_payment(self, req: PaymentRequest) -> ProviderResult:
        reference = str(uuid.uuid4())
        headers = {**self._headers(), "Authorization": f"Bearer {self._access_token()}",
                   "X-Reference-Id": reference, "Content-Type": "application/json"}
        if settings.momo_callback_host:
            headers["X-Callback-Url"] = f"{settings.momo_callback_host.rstrip('/')}/webhooks/payments/momo/{req.payment_id}"
        body = {
            "amount": str(int(req.amount)) if req.amount == int(req.amount) else str(req.amount),
            "currency": settings.momo_currency_override or req.currency,
            "externalId": req.payment_id,
            "payer": {"partyIdType": "MSISDN", "partyId": req.payer_phone},
            "payerMessage": req.description[:160],
            "payeeNote": req.order_number,
        }
        self._request("POST", "/collection/v1_0/requesttopay", headers=headers, json=body)
        log_event(logger, "momo.request_to_pay", operation="momo.request_to_pay", reference=reference)
        return ProviderResult(reference=reference, status="pending", raw={"request": {**body, "payer": "***"}})

    def get_status(self, reference: str) -> ProviderResult:
        headers = {**self._headers(), "Authorization": f"Bearer {self._access_token()}"}
        data = self._request("GET", f"/collection/v1_0/requesttopay/{reference}", headers=headers).json()
        status = _STATUS_MAP.get(str(data.get("status", "")).upper(), "pending")
        reason = data.get("reason")
        return ProviderResult(reference=reference, status=status, raw={"status_response": data},
                              failure_reason=str(reason) if reason else None)
