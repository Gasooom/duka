"""Usage metering: writes the usage ledger (models/usage.py, usage_events): real AI model calls and WhatsApp traffic.

Every event is written in its OWN short transaction, on its own connection, right after the external call it
records: the cost was incurred whatever happens next, so the event must survive a rollback of the caller's
transaction (a failed turn is processed again and makes new calls, recorded as new events). Writing it can never
break a customer reply either: a failure is logged as usage.record_failed with the event's fields.

Accepted P0 limitation: this is best effort, with no queue behind it. If the process dies after the provider has
answered and before this transaction commits, or this write fails, that one call is missing from the ledger (a
failed write is still in the log). A rollback of the caller's transaction never removes an event once written.

The insert takes FOR KEY SHARE on the business row (foreign key), which does not conflict with the lock order-number
allocation holds on it (FOR NO KEY UPDATE), so the caller's open transaction never blocks it.

WhatsApp (record_wa_*, at the end of this module) follows the same rule with one split, by what the event describes:
a send attempt happened outside the database, so it is written in its own transaction right after the adapter
returns; an inbound message and a late failure are changes of state in the caller's transaction, so their event is
written in that same transaction, inside a SAVEPOINT: it commits or rolls back with the state it describes, and a
ledger error can never abort the transaction around it."""
import uuid
from typing import Any

from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from app.core.logging import get_logger, log_event, safe_error
from app.integrations.whatsapp.adapters import metering_enabled
from app.integrations.whatsapp.market import calling_code
from app.models import UsageEvent
from app.repositories.repos import UsageEventRepo
from app.services import pricing

logger = get_logger(__name__)
LLM_CALL = "llm_call"
_INT4_MAX = 2**31 - 1


def _count(value: Any) -> int | None:
    """A provider-reported count the ledger can hold, else None (unknown)."""
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _INT4_MAX else None


def _text(value: Any, limit: int) -> str | None:
    return str(value)[:limit] if value else None


def record_llm_call(bind: Engine | Connection, business_id: uuid.UUID, *, idempotency_key: str, source_type: str,
                    source_id: uuid.UUID | None, provider: str, model: str | None, configured_model: str | None,
                    status: str, input_tokens: int | None = None, output_tokens: int | None = None,
                    tool_calls: int = 0, attempts: int = 1) -> bool:
    """Record one real AI model call of `business_id` (resolved by the caller from server-side state, never from
    input). True when written; False when this key is already recorded or the write failed (logged)."""
    event: dict[str, Any] = dict(
        kind=LLM_CALL, idempotency_key=idempotency_key, source_type=source_type, source_id=source_id, status=status,
        units=1, input_tokens=_count(input_tokens), output_tokens=_count(output_tokens),
        tool_calls=_count(tool_calls) or 0, attempts=_count(attempts) or 0, provider=_text(provider, 40),
        model=_text(model, 100), configured_model=_text(configured_model, 100))
    try:
        price = pricing.llm_call_price(provider=event["provider"], model=event["model"],
                                       configured_model=event["configured_model"], input_tokens=event["input_tokens"],
                                       output_tokens=event["output_tokens"], failed=status == "error")
    except Exception as exc:  # an unusable price list: the event is still recorded, unpriced
        log_event(logger, "usage.pricing_failed", 40, operation="usage", status="error", error=safe_error(exc))
        price = pricing.UNPRICED
    event.update(cost_micros=price.cost_micros, currency=price.currency, price_version=price.price_version)
    try:
        # bind.engine: a new connection even when the caller's session is bound to a connection, never its transaction
        with Session(bind=bind.engine) as db:
            written = UsageEventRepo(db, business_id).record(**event)
            db.commit()
        return written
    except Exception as exc:  # metering must never break the reply; the log line keeps what was not stored
        # (token counts under other names: log keys containing "token" are redacted)
        log_event(logger, "usage.record_failed", 40, operation="usage", status="error", error=safe_error(exc),
                  business_id=str(business_id), source_id=str(source_id) if source_id else None,
                  event_status=event["status"], input_count=event["input_tokens"], output_count=event["output_tokens"],
                  **{k: v for k, v in event.items()
                     if k not in ("source_id", "status", "input_tokens", "output_tokens")})
        return False


# ---------------------------------------------------------------- WhatsApp
# `business_id` always comes from server-side state (the account a webhook arrived on, the claimed outbox row), never
# from input. No phone number is stored: `market` is the recipient's country calling code.
WA_IN, WA_OUT, WA_ALERT = "wa_in", "wa_out", "wa_alert"
WA_PROVIDER = "whatsapp"
# Inbound message types whose usage is understood: what a customer composes and sends. Reactions, WhatsApp's own
# system notices and anything unknown are not counted until their treatment is decided.
METERED_INBOUND_TYPES = frozenset({"text", "interactive", "button", "image", "audio", "video", "document", "sticker",
                                   "location", "contacts"})


def _wa_event(kind: str, key: str, *, source_type: str, source_id: uuid.UUID, status: str, is_real: bool | None,
              attempts: int = 0, units: int = 1, message_kind: str | None = None, template_name: str | None = None,
              recipient: str | None = None) -> dict[str, Any]:
    event: dict[str, Any] = dict(
        kind=kind, idempotency_key=key, source_type=source_type, source_id=source_id, status=status, units=units,
        attempts=attempts, provider=WA_PROVIDER, is_real=is_real, message_kind=message_kind,
        template_name=_text(template_name, 100) if message_kind == "template" else None,
        market=calling_code(recipient))
    try:
        price = pricing.whatsapp_price(kind=kind, status=status, is_real=is_real, message_kind=message_kind,
                                       template_name=event["template_name"], market=event["market"])
    except Exception as exc:  # an unusable price list: the event is still recorded, unpriced
        log_event(logger, "usage.pricing_failed", 40, operation="usage", status="error", error=safe_error(exc))
        price = pricing.UNPRICED
    event.update(cost_micros=price.cost_micros, currency=price.currency, price_version=price.price_version)
    return event


def _log_record_failed(exc: Exception, business_id: uuid.UUID, event: dict[str, Any]) -> None:
    log_event(logger, "usage.record_failed", 40, operation="usage", status="error", error=safe_error(exc),
              business_id=str(business_id), source_id=str(event["source_id"]), event_status=event["status"],
              **{k: v for k, v in event.items() if k not in ("source_id", "status")})


def _write_independently(bind: Engine | Connection, business_id: uuid.UUID, event: dict[str, Any]) -> bool:
    try:
        with Session(bind=bind.engine) as db:  # a new connection, never the caller's transaction
            written = UsageEventRepo(db, business_id).record(**event)
            db.commit()
        return written
    except Exception as exc:
        _log_record_failed(exc, business_id, event)
        return False


def _write_in_transaction(db: Session, business_id: uuid.UUID, event: dict[str, Any]) -> bool:
    try:
        with db.begin_nested():
            return UsageEventRepo(db, business_id).record(**event)
    except Exception as exc:
        _log_record_failed(exc, business_id, event)
        return False


def record_wa_inbound(db: Session, business_id: uuid.UUID, *, wamid: str, message_id: uuid.UUID, is_real: bool,
                      sender: str | None, message_type: str) -> bool:
    """Record one inbound customer message, in the transaction that stored it (call only when the message row was
    really inserted: a duplicate wamid is never recorded twice). `is_real`: the webhook's signature was verified."""
    if message_type not in METERED_INBOUND_TYPES or not metering_enabled():
        return False
    event = _wa_event(WA_IN, f"wa_in:{wamid}", source_type="message", source_id=message_id, status="received",
                      is_real=is_real, recipient=sender)
    return _write_in_transaction(db, business_id, event)


def record_wa_send(bind: Engine | Connection, business_id: uuid.UUID, *, kind: str, source_type: str,
                   source_id: uuid.UUID, attempt: int, status: str, is_real: bool | None, message_kind: str | None,
                   template_name: str | None, recipient: str | None) -> bool:
    """Record one attempt to send a customer message (wa_out) or an owner alert (wa_alert), keyed by the outbox
    attempt number the atomic send claim handed out: success | failed, or unknown for a send interrupted midway
    (the same key, so a result already written wins). An attempt may hold several HTTP requests inside the Cloud
    adapter. Call only for attempts that reached an adapter that sends. `is_real` may be None only for status
    unknown, when whether the interrupted attempt was real cannot be told."""
    event = _wa_event(kind, f"{kind}:{source_id}:{attempt}", source_type=source_type, source_id=source_id,
                      status=status, is_real=is_real, attempts=attempt, message_kind=message_kind,
                      template_name=template_name, recipient=recipient)
    return _write_independently(bind, business_id, event)


def record_wa_late_failure(db: Session, business_id: uuid.UUID, *, kind: str, source_type: str,
                           source_id: uuid.UUID, attempt: int, verified: bool, message_kind: str | None,
                           recipient: str | None) -> bool:
    """WhatsApp accepted a message (an earlier success event) and then reported it failed. The success event is
    never edited; this adds one event with units 0 (a correction, not a send) for the same message. It repeats what
    the attempt it corrects recorded (real or not, template, market) when that event exists, else what is known now
    (`verified`: this status webhook's signature was verified)."""
    if not metering_enabled():
        return False
    key = f"wa_late_fail:{source_id}"
    try:
        with db.begin_nested():  # a read failing inside the caller's transaction must not abort it either
            original = UsageEventRepo(db, business_id).first(
                UsageEvent.idempotency_key == f"{kind}:{source_id}:{attempt}")
    except Exception as exc:
        log_event(logger, "usage.record_failed", 40, operation="usage", status="error", error=safe_error(exc),
                  business_id=str(business_id), source_id=str(source_id), kind=kind, idempotency_key=key)
        return False
    if original is not None:
        event = _wa_event(kind, key, source_type=source_type, source_id=source_id, status="late_failed", units=0,
                          is_real=verified if original.is_real is None else original.is_real, attempts=attempt,
                          message_kind=original.message_kind,
                          template_name=original.template_name)
        event["market"] = original.market
    else:
        event = _wa_event(kind, key, source_type=source_type, source_id=source_id, status="late_failed", units=0,
                          is_real=verified, attempts=attempt, message_kind=message_kind, recipient=recipient)
    return _write_in_transaction(db, business_id, event)
