"""Parse WhatsApp Cloud API webhook payloads into normalized inbound events.

Payload reference: https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/payload-examples
"""
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class InboundMessage:
    phone_number_id: str      # which business number received it -> tenant resolution
    from_number: str          # customer's WhatsApp number (wa_id)
    wa_message_id: str
    type: str                 # text | interactive | image | audio | ...
    text: str | None
    profile_name: str | None = None
    timestamp: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    # Set by the server at ingest, never read from the payload: the webhook's X-Hub-Signature-256 was verified with
    # the app secret, so the message really came from Meta. False for the dev simulator, tests and an unsigned
    # development webhook. Decides usage_events.is_real for inbound messages.
    signature_verified: bool = False

    def to_payload(self) -> dict[str, Any]:
        """JSON form stored in webhook_events.payload."""
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "InboundMessage":
        return cls(**payload)


@dataclass
class StatusUpdate:
    phone_number_id: str
    wa_message_id: str
    status: str  # sent | delivered | read | failed
    recipient: str | None = None
    error_code: int | None = None  # on 'failed', e.g. 131047 = more than 24 h since the customer's last message
    error_title: str | None = None


def parse_webhook(payload: dict[str, Any]) -> tuple[list[InboundMessage], list[StatusUpdate]]:
    messages: list[InboundMessage] = []
    statuses: list[StatusUpdate] = []
    if payload.get("object") != "whatsapp_business_account":
        return messages, statuses
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            phone_number_id = (value.get("metadata") or {}).get("phone_number_id")
            if not phone_number_id:
                continue
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts", []) or []}
            for m in value.get("messages", []) or []:
                mtype = m.get("type", "unknown")
                text = None
                if mtype == "text":
                    text = (m.get("text") or {}).get("body")
                elif mtype == "interactive":
                    inter = m.get("interactive") or {}
                    reply = inter.get("button_reply") or inter.get("list_reply") or {}
                    text = reply.get("title") or reply.get("id")
                elif mtype == "button":
                    text = (m.get("button") or {}).get("text")
                messages.append(InboundMessage(
                    phone_number_id=str(phone_number_id), from_number=str(m.get("from", "")),
                    wa_message_id=str(m.get("id", "")), type=mtype, text=text,
                    profile_name=names.get(m.get("from")), timestamp=m.get("timestamp"), raw=m,
                ))
            for s in value.get("statuses", []) or []:
                error = ((s.get("errors") or [None])[0]) or {}
                statuses.append(StatusUpdate(phone_number_id=str(phone_number_id), wa_message_id=str(s.get("id")),
                                             status=str(s.get("status")), recipient=s.get("recipient_id"),
                                             error_code=meta_error_code(error.get("code")),
                                             error_title=error.get("title") or error.get("message")))
    return messages, statuses


def meta_error_code(value: Any) -> int | None:
    """Meta's numeric error code, whether sent as a number or a string."""
    return int(value) if isinstance(value, int) or (isinstance(value, str) and value.isdigit()) else None


def build_text_webhook(phone_number_id: str, display_number: str, from_number: str, text: str,
                       wa_message_id: str, profile_name: str | None = None, timestamp: str | None = None) -> dict:
    """Build a payload in the exact shape Meta sends. Used by the dev simulator and tests so
    simulated messages go through the identical parsing/processing path."""
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "WABA_ID",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": display_number, "phone_number_id": phone_number_id},
                    "contacts": [{"profile": {"name": profile_name or "Customer"}, "wa_id": from_number}],
                    "messages": [{"from": from_number, "id": wa_message_id, "timestamp": timestamp or "0",
                                  "type": "text", "text": {"body": text}}],
                },
            }],
        }],
    }
