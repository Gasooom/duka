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


class WhatsAppAdapter(ABC):
    mode: str

    @abstractmethod
    def send_text(self, to: str, body: str) -> SendResult: ...


class DevWhatsAppAdapter(WhatsAppAdapter):
    mode = "dev"

    def send_text(self, to: str, body: str) -> SendResult:
        return SendResult(ok=True, wa_message_id=f"dev.{uuid.uuid4().hex}", delivery_status="simulated")


class CloudWhatsAppAdapter(WhatsAppAdapter):
    mode = "cloud"

    def __init__(self, phone_number_id: str, access_token: str, client: httpx.Client | None = None,
                 max_attempts: int = 3):
        self.url = f"{settings.whatsapp_graph_base_url}/{settings.whatsapp_api_version}/{phone_number_id}/messages"
        self.token = access_token
        self.client = client or httpx.Client(timeout=settings.whatsapp_timeout_seconds)
        self.max_attempts = max_attempts

    def send_text(self, to: str, body: str) -> SendResult:
        payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, "type": "text",
                   "text": {"preview_url": False, "body": body[:WHATSAPP_TEXT_LIMIT]}}
        last_error = None
        retryable = False
        for attempt in range(1, self.max_attempts + 1):
            try:
                r = self.client.post(self.url, json=payload, headers={"Authorization": f"Bearer {self.token}"})
                if r.status_code == 200:
                    wamid = (r.json().get("messages") or [{}])[0].get("id")
                    return SendResult(ok=True, wa_message_id=wamid, delivery_status="sent")
                last_error = f"HTTP {r.status_code}: {r.text[:300]}"
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
                          retryable=retryable)


class _MisconfiguredAdapter(WhatsAppAdapter):
    """Cloud account without a token: fail loudly instead of pretending to send."""
    mode = "cloud"

    def send_text(self, to: str, body: str) -> SendResult:
        return SendResult(ok=False, wa_message_id=None, delivery_status="failed",
                          error="WhatsApp access token missing for this account")


_adapter_override: WhatsAppAdapter | None = None


def set_adapter_override(adapter: WhatsAppAdapter | None) -> None:
    """Test hook to capture outbound messages."""
    global _adapter_override
    _adapter_override = adapter


def get_adapter(account: WhatsAppAccount) -> WhatsAppAdapter:
    if _adapter_override is not None:
        return _adapter_override
    if settings.whatsapp_force_dev or account.mode == "dev":
        return DevWhatsAppAdapter()
    if not account.access_token_encrypted:
        log_event(logger, "whatsapp.missing_token", operation="whatsapp.send", status="error",
                  phone_number_id=account.phone_number_id)
        return _MisconfiguredAdapter()
    return CloudWhatsAppAdapter(account.phone_number_id, decrypt_secret(account.access_token_encrypted))
