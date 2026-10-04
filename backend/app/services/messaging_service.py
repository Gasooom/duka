"""Outbound WhatsApp messages via a transactional outbox.

`send_to_customer` only *queues* the message: it is a `messages` row (delivery_status='queued') written in the
caller's transaction, next to the state it describes (cart, order, payment...). It is sent after that
transaction commits — by `commit_and_deliver` right away, or by the outbox worker as a safety net. So a customer
can never receive "Order KF-00012 placed" for an order that was rolled back.

Delivery states: queued -> sending -> sent | simulated, or -> retry (transient error, backoff) -> ... -> failed.
A message found stuck in 'sending' (worker died mid-request) is marked failed and flagged for staff instead of
being re-sent, because WhatsApp has no idempotency key and the customer may already have it.
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import bind_context, clear_context, get_logger, log_event, log_operation
from app.db.session import SessionLocal
from app.integrations.whatsapp.adapters import SendResult, get_adapter
from app.models import Conversation, Message, WhatsAppAccount
from app.repositories.repos import ConversationRepo, WhatsAppAccountRepo
from app.services.conversation_service import ConversationService

logger = get_logger(__name__)
OUTBOX_KEY = "duka_outbox"
RETRY_DELAYS_SECONDS = (10, 60, 300, 900)


def send_to_customer(db: Session, business_id: uuid.UUID, conversation: Conversation, text: str, *,
                     role: str = "assistant", agent_run_id: uuid.UUID | None = None,
                     metadata: dict | None = None) -> Message:
    """Queue a message to the conversation's customer. Sent only after the caller's transaction commits."""
    msg = ConversationService(db, business_id).add_message(
        conversation, role, text, delivery_status="queued", agent_run_id=agent_run_id, metadata=metadata)
    db.info.setdefault(OUTBOX_KEY, []).append(msg.id)
    return msg


def commit_and_deliver(db: Session, session_factory=SessionLocal) -> None:
    """Commit the caller's transaction, then send the messages it queued."""
    ids = db.info.pop(OUTBOX_KEY, [])
    db.commit()
    deliver(ids, session_factory)


def deliver(message_ids: list[uuid.UUID], session_factory=SessionLocal) -> None:
    for mid in message_ids:
        try:
            _deliver_one(mid, session_factory)
        except Exception as exc:  # never let one message break the caller; the worker retries 'queued' rows
            log_event(logger, "outbox.deliver_crashed", 40, operation="outbox.deliver", status="error",
                      error=repr(exc)[:300])


def _deliver_one(message_id: uuid.UUID, session_factory) -> None:
    db = session_factory()
    try:
        # Claim atomically so the inline path and the worker never both send the same message.
        claimed = db.execute(
            update(Message).where(Message.id == message_id, Message.delivery_status.in_(("queued", "retry")))
            .values(delivery_status="sending", send_attempts=Message.send_attempts + 1,
                    send_started_at=datetime.now(timezone.utc), next_send_at=None)
            .returning(Message.business_id)).first()
        if claimed is None:
            db.rollback()
            return
        db.commit()
        business_id = claimed[0]
        bind_context(business_id=business_id)
        msg = db.get(Message, message_id)
        conv = ConversationRepo(db, business_id).get(msg.conversation_id)
        account = WhatsAppAccountRepo(db, business_id).first(WhatsAppAccount.is_active.is_(True))
        if account is None:
            result = SendResult(ok=False, wa_message_id=None, delivery_status="failed",
                                error="No WhatsApp account connected")
        else:
            adapter = get_adapter(account)
            with log_operation(logger, "whatsapp.send", mode=adapter.mode, attempt=msg.send_attempts) as ctx:
                try:
                    result = adapter.send_text(conv.customer.whatsapp_number, msg.content)
                except Exception as exc:  # unexpected adapter bug/network edge: treat as transient
                    result = SendResult(ok=False, wa_message_id=None, delivery_status="failed",
                                        error=f"{type(exc).__name__}: {exc}"[:300], retryable=True)
                ctx["status"] = "ok" if result.ok else "error"
        _record_result(msg, conv, result)
        db.commit()
    finally:
        db.close()
        clear_context()


def _record_result(msg: Message, conv: Conversation | None, result: SendResult) -> None:
    attrs = dict(msg.attributes or {})
    if result.ok:
        msg.delivery_status = result.delivery_status
        msg.wa_message_id = result.wa_message_id
        attrs.pop("error", None)
    elif result.retryable and msg.send_attempts < settings.outbox_max_attempts:
        delay = RETRY_DELAYS_SECONDS[min(msg.send_attempts - 1, len(RETRY_DELAYS_SECONDS) - 1)]
        msg.delivery_status = "retry"
        msg.next_send_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        attrs["error"] = result.error
    else:
        msg.delivery_status = "failed"
        attrs["error"] = result.error
        if conv is not None:
            conv.needs_attention = True  # a customer did not get our message: staff should know
        log_event(logger, "outbox.failed", 40, operation="outbox.deliver", status="error", error=result.error,
                  attempts=msg.send_attempts)
    msg.attributes = attrs


def deliver_due(session_factory=SessionLocal, limit: int = 50) -> int:
    """Send queued messages whose inline delivery never happened and retries that are due. System-level:
    each row carries its own business_id, which is the tenant for the send."""
    now = datetime.now(timezone.utc)
    with session_factory() as db:
        ids = list(db.scalars(
            select(Message.id).where(Message.delivery_status.in_(("queued", "retry")),
                                     or_(Message.next_send_at.is_(None), Message.next_send_at <= now))
            .order_by(Message.created_at).limit(limit)))
    deliver(ids, session_factory)
    return len(ids)


def recover_stale_sends(session_factory=SessionLocal) -> int:
    """Messages stuck in 'sending' (the process died during the HTTP call): outcome unknown, so flag, don't resend."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.outbox_sending_timeout_seconds)
    with session_factory() as db:
        stale = list(db.scalars(select(Message).where(Message.delivery_status == "sending",
                                                       Message.send_started_at < cutoff).with_for_update(skip_locked=True)))
        for msg in stale:
            conv = ConversationRepo(db, msg.business_id).get(msg.conversation_id)
            _record_result(msg, conv, SendResult(
                ok=False, wa_message_id=None, delivery_status="failed",
                error="Delivery outcome unknown (worker stopped while sending); not re-sent to avoid a duplicate"))
        db.commit()
        return len(stale)
