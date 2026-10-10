"""Outbound WhatsApp adapters.

CloudWhatsAppAdapter  -> real Meta Graph API (POST /{phone_number_id}/messages)
DevWhatsAppAdapter    -> no network; the message is stored with delivery_status='simulated'
                         so the dashboard simulator shows it. Zero cost for development.
"""
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.core.logging import get_logger, log_event
from app.core.security import decrypt_secret
from app.integrations.whatsapp.parser import meta_error_code
from app.models import WhatsAppAccount

logger = get_logger(__name__)
WHATSAPP_TEXT_LIMIT = 4096


@dataclass
class SendResult:
    ok: bool
    wa_message_id: str | None
    delivery_status: str  # sent | simulated | failed
    error: str | None = None
    retryable: bool = False  # transient failure (429/5xx/network): the outbox may try again later
    error_code: int | None = None  # Meta's error code when it rejected the request
    reason: str | None = None  # machine-readable failure reason, e.g. "outside_24h_window"
    http_attempts: int | None = None  # HTTP requests made for this send (Cloud adapter); None = not tracked


META_WINDOW_ERROR = 131047  # Meta: more than 24 hours since the customer's last message (templates only)
OUTSIDE_WINDOW = "outside_24h_window"


class WhatsAppAdapter(ABC):
    mode: str
    # Usage metering (usage_events). is_real: a send through this adapter really reaches WhatsApp. metered: a send
    # through it is tenant usage and is recorded (False: nothing is ever sent, or the evaluation harness's capture).
    is_real: bool = False
    metered: bool = True

    @abstractmethod
    def send_text(self, to: str, body: str) -> SendResult: ...

    def send_template(self, to: str, name: str, language: str, params: list[str]) -> SendResult:
        """Approved template message (required by Meta outside the 24h customer-service window).
        Adapters without template support send the parameters as text."""
        return self.send_text(to, "\n".join(params))


class DevWhatsAppAdapter(WhatsAppAdapter):
    mode = "dev"

    def send_text(self, to: str, body: str) -> SendResult:
        return SendResult(ok=True, wa_message_id=f"dev.{uuid.uuid4().hex}", delivery_status="simulated")


class CloudWhatsAppAdapter(WhatsAppAdapter):
    mode = "cloud"
    is_real = True

    def __init__(self, phone_number_id: str, access_token: str, client: httpx.Client | None = None,
                 max_attempts: int = 3):
        self.url = f"{settings.whatsapp_graph_base_url}/{settings.whatsapp_api_version}/{phone_number_id}/messages"
        self.token = access_token
        self.client = client or httpx.Client(timeout=settings.whatsapp_timeout_seconds)
        self.max_attempts = max_attempts

    def send_text(self, to: str, body: str) -> SendResult:
        return self._post({"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, "type": "text",
                           "text": {"preview_url": False, "body": body[:WHATSAPP_TEXT_LIMIT]}})

    def send_template(self, to: str, name: str, language: str, params: list[str]) -> SendResult:
        # Template parameters cannot contain newlines/tabs or more than 4 consecutive spaces (Meta rule).
        clean = [" ".join(p.split())[:1000] for p in params]
        return self._post({"messaging_product": "whatsapp", "recipient_type": "individual", "to": to,
                           "type": "template", "template": {
                               "name": name, "language": {"code": language},
                               "components": [{"type": "body", "parameters": [{"type": "text", "text": p}
                                                                              for p in clean]}]}})

    def _post(self, payload: dict) -> SendResult:
        last_error = None
        retryable = False
        error_code = None
        attempt = 0
        for attempt in range(1, self.max_attempts + 1):
            try:
                r = self.client.post(self.url, json=payload, headers={"Authorization": f"Bearer {self.token}"})
                if r.status_code == 200:
                    wamid = (r.json().get("messages") or [{}])[0].get("id")
                    return SendResult(ok=True, wa_message_id=wamid, delivery_status="sent", http_attempts=attempt)
                last_error = f"HTTP {r.status_code}: {r.text[:300]}"
                error_code = _error_code(r)
                retryable = r.status_code in (429, 500, 502, 503, 504)
                if not retryable:
                    break  # permanent error (bad token, invalid recipient...) -> don't retry
            except httpx.TransportError as exc:  # timeouts, connection errors
                last_error = f"{type(exc).__name__}: {exc}"
                retryable = True
            if attempt < self.max_attempts:
                time.sleep(0.4 * (2 ** (attempt - 1)))
        log_event(logger, "whatsapp.send_failed", operation="whatsapp.send", status="error", error=last_error)
        return SendResult(ok=False, wa_message_id=None, delivery_status="failed", error=last_error,
                          retryable=retryable, error_code=error_code, http_attempts=attempt,
                          reason=OUTSIDE_WINDOW if error_code == META_WINDOW_ERROR else None)


def _error_code(r: httpx.Response) -> int | None:
    try:
        body = r.json()
    except ValueError:
        return None
    return meta_error_code((body.get("error") or {}).get("code")) if isinstance(body, dict) else None


class _MisconfiguredAdapter(WhatsAppAdapter):
    """Cannot send (no token, or simulation in production): fail loudly instead of pretending to send."""
    mode = "cloud"
    metered = False  # nothing is ever sent, so there is no usage

    def __init__(self, reason: str = "WhatsApp access token missing for this account"):
        self.reason = reason

    def send_text(self, to: str, body: str) -> SendResult:
        return SendResult(ok=False, wa_message_id=None, delivery_status="failed", error=self.reason)


_adapter_override: WhatsAppAdapter | None = None


def set_adapter_override(adapter: WhatsAppAdapter | None) -> None:
    """Test hook to capture outbound messages."""
    global _adapter_override
    _adapter_override = adapter


def metering_enabled() -> bool:
    """Whether WhatsApp traffic is recorded as tenant usage: always, unless a test hook replaced WhatsApp with an
    adapter that is not metered (the evaluation harness's capture: platform activity, never a tenant's usage)."""
    return _adapter_override is None or _adapter_override.metered


def get_adapter(account: WhatsAppAccount) -> WhatsAppAdapter:
    if _adapter_override is not None:
        return _adapter_override
    if settings.is_production and (settings.whatsapp_force_dev or account.mode == "dev"):
        # Never pretend to deliver in production: a simulated send would hide that customers get nothing.
        return _MisconfiguredAdapter("Simulated WhatsApp sending is disabled in production")
    if settings.whatsapp_force_dev or account.mode == "dev":
        return DevWhatsAppAdapter()
    if not account.access_token_encrypted:
        log_event(logger, "whatsapp.missing_token", operation="whatsapp.send", status="error",
                  phone_number_id=account.phone_number_id)
        return _MisconfiguredAdapter()
    return CloudWhatsAppAdapter(account.phone_number_id, decrypt_secret(account.access_token_encrypted))
