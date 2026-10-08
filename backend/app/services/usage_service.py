"""Usage metering: writes the usage ledger (models/usage.py, usage_events).

Every event is written in its OWN short transaction, on its own connection, right after the external call it
records: the cost was incurred whatever happens next, so the event must survive a rollback of the caller's
transaction (a failed turn is processed again and makes new calls, recorded as new events). Writing it can never
break a customer reply either: a failure is logged as usage.record_failed with the event's fields.

Accepted P0 limitation: this is best effort, with no queue behind it. If the process dies after the provider has
answered and before this transaction commits, or this write fails, that one call is missing from the ledger (a
failed write is still in the log). A rollback of the caller's transaction never removes an event once written.

The insert takes FOR KEY SHARE on the business row (foreign key), which does not conflict with the lock order-number
allocation holds on it (FOR NO KEY UPDATE), so the caller's open transaction never blocks it."""
import uuid
from typing import Any

from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from app.core.logging import get_logger, log_event, safe_error
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
