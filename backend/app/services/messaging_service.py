"""Outbound WhatsApp messages via a transactional outbox.

`send_to_customer` only *queues* the message: it is a `messages` row (delivery_status='queued') written in the
caller's transaction, next to the state it describes (cart, order, payment...). `notify_owner` does the same for
the shop owner (a `notifications` row). Both are sent after the transaction commits — by `commit_and_deliver`
right away, or by the worker as a safety net. So nobody is told "Order KF-00012 placed" for a rolled-back order.

Delivery states: queued -> sending -> sent | simulated, or -> retry (transient error, backoff) -> ... -> failed.
A row found stuck in 'sending' (worker died mid-request) is marked failed and flagged instead of being re-sent,
because WhatsApp has no idempotency key and the recipient may already have it.
"""
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import bind_context, clear_context, get_logger, log_event, log_operation
from app.db.session import SessionLocal
from app.integrations.whatsapp.adapters import SendResult, get_adapter
from app.models import Conversation, Message, Notification, WhatsAppAccount
from app.repositories.repos import ConversationRepo, NotificationRepo, SettingsRepo, WhatsAppAccountRepo
from app.services.conversation_service import ConversationService

logger = get_logger(__name__)
OUTBOX_KEY = "duka_outbox"
NOTIFY_KEY = "duka_notifications"
RETRY_DELAYS_SECONDS = (10, 60, 300, 900)


@dataclass
class Outbox:
    messages: list[uuid.UUID] = field(default_factory=list)
    notifications: list[uuid.UUID] = field(default_factory=list)


def send_to_customer(db: Session, business_id: uuid.UUID, conversation: Conversation, text: str, *,
                     role: str = "assistant", agent_run_id: uuid.UUID | None = None,
                     metadata: dict | None = None) -> Message:
    """Queue a message to the conversation's customer. Sent only after the caller's transaction commits."""
    msg = ConversationService(db, business_id).add_message(
        conversation, role, text, delivery_status="queued", agent_run_id=agent_run_id, metadata=metadata)
    db.info.setdefault(OUTBOX_KEY, []).append(msg.id)
    return msg


def notify_owner(db: Session, business_id: uuid.UUID, kind: str, body: str, *, entity_type: str | None = None,
                 entity_id: uuid.UUID | None = None) -> Notification:
    """Queue a notification to the owner's WhatsApp number. Always recorded (the dashboard lists them); only
    sent when an owner_notification_phone is configured, otherwise status='skipped'."""
    s = SettingsRepo(db, business_id).first()
    phone = s.owner_notification_phone if s else None
    n = NotificationRepo(db, business_id).add(kind=kind, recipient=phone, body=body[:4000], entity_type=entity_type,
                                              entity_id=entity_id, status="queued" if phone else "skipped", attempts=0)
    if phone:
        db.info.setdefault(NOTIFY_KEY, []).append(n.id)
    return n


def take_outbox(db: Session) -> Outbox:
    return Outbox(db.info.pop(OUTBOX_KEY, []), db.info.pop(NOTIFY_KEY, []))


def commit_and_deliver(db: Session, session_factory=SessionLocal) -> None:
    """Commit the caller's transaction, then send what it queued."""
    outbox = take_outbox(db)
    db.commit()
    deliver_outbox(outbox, session_factory)


def deliver_outbox(outbox: Outbox, session_factory=SessionLocal) -> None:
    deliver(outbox.messages, session_factory)
    for nid in outbox.notifications:
        _safely(_deliver_notification, nid, session_factory)


def deliver(message_ids: list[uuid.UUID], session_factory=SessionLocal) -> None:
    for mid in message_ids:
        _safely(_deliver_one, mid, session_factory)


def _safely(fn, row_id, session_factory) -> None:
    try:
        fn(row_id, session_factory)
    except Exception as exc:  # never let one message break the caller; the worker retries queued rows
        log_event(logger, "outbox.deliver_crashed", 40, operation="outbox.deliver", status="error",
                  error=repr(exc)[:300])


def _active_account(db: Session, business_id: uuid.UUID) -> WhatsAppAccount | None:
    return WhatsAppAccountRepo(db, business_id).first(WhatsAppAccount.is_active.is_(True))


def _send(db: Session, business_id: uuid.UUID, to: str, body: str, attempt: int, *,
          template: tuple[str, str] | None = None) -> SendResult:
    account = _active_account(db, business_id)
    if account is None:
        return SendResult(ok=False, wa_message_id=None, delivery_status="failed", error="No WhatsApp account connected")
    adapter = get_adapter(account)
    with log_operation(logger, "whatsapp.send", mode=adapter.mode, attempt=attempt) as ctx:
        try:
            result = (adapter.send_template(to, template[0], template[1], [body]) if template
                      else adapter.send_text(to, body))
        except Exception as exc:  # unexpected adapter bug/network edge: treat as transient
            result = SendResult(ok=False, wa_message_id=None, delivery_status="failed",
                                error=f"{type(exc).__name__}: {exc}"[:300], retryable=True)
        ctx["status"] = "ok" if result.ok else "error"
    return result


def _next_state(result: SendResult, attempts: int) -> tuple[str, datetime | None]:
    if result.ok:
        return result.delivery_status, None
    if result.retryable and attempts < settings.outbox_max_attempts:
        delay = RETRY_DELAYS_SECONDS[min(attempts - 1, len(RETRY_DELAYS_SECONDS) - 1)]
        return "retry", datetime.now(timezone.utc) + timedelta(seconds=delay)
    return "failed", None


# ---------------------------------------------------------------- customer messages
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
        result = _send(db, business_id, conv.customer.whatsapp_number, msg.content, msg.send_attempts)
        _record_message_result(msg, conv, result)
        db.commit()
    finally:
        db.close()
        clear_context()


def _record_message_result(msg: Message, conv: Conversation | None, result: SendResult) -> None:
    attrs = dict(msg.attributes or {})
    msg.delivery_status, msg.next_send_at = _next_state(result, msg.send_attempts)
    if result.ok:
        msg.wa_message_id = result.wa_message_id
        attrs.pop("error", None)
    else:
        attrs["error"] = result.error
    if msg.delivery_status == "failed":
        if conv is not None:
            conv.needs_attention = True  # a customer did not get our message: staff should know
        log_event(logger, "outbox.failed", 40, operation="outbox.deliver", status="error", error=result.error,
                  attempts=msg.send_attempts)
    msg.attributes = attrs


# ---------------------------------------------------------------- owner notifications
def _deliver_notification(notification_id: uuid.UUID, session_factory) -> None:
    db = session_factory()
    try:
        claimed = db.execute(
            update(Notification).where(Notification.id == notification_id,
                                       Notification.status.in_(("queued", "retry")))
            .values(status="sending", attempts=Notification.attempts + 1,
                    send_started_at=datetime.now(timezone.utc), next_send_at=None)
            .returning(Notification.business_id)).first()
        if claimed is None:
            db.rollback()
            return
        db.commit()
        business_id = claimed[0]
        bind_context(business_id=business_id)
        n = db.get(Notification, notification_id)
        s = SettingsRepo(db, business_id).first()
        template = (s.owner_notification_template, s.owner_notification_template_language) \
            if s and s.owner_notification_template else None
        result = _send(db, business_id, n.recipient, n.body, n.attempts, template=template)
        n.status, n.next_send_at = _next_state(result, n.attempts)
        n.wa_message_id = result.wa_message_id if result.ok else None
        n.error = None if result.ok else result.error
        if n.status == "failed":
            log_event(logger, "notification.failed", 40, operation="outbox.notify", status="error",
                      error=result.error, kind=n.kind)
        db.commit()
    finally:
        db.close()
        clear_context()


# ---------------------------------------------------------------- worker sweeps
def deliver_due(session_factory=SessionLocal, limit: int = 50) -> int:
    """Send queued rows whose inline delivery never happened and retries that are due. System-level:
    each row carries its own business_id, which is the tenant for the send."""
    now = datetime.now(timezone.utc)
    with session_factory() as db:
        mids = list(db.scalars(
            select(Message.id).where(Message.delivery_status.in_(("queued", "retry")),
                                     or_(Message.next_send_at.is_(None), Message.next_send_at <= now))
            .order_by(Message.created_at).limit(limit)))
        nids = list(db.scalars(
            select(Notification.id).where(Notification.status.in_(("queued", "retry")),
                                          or_(Notification.next_send_at.is_(None), Notification.next_send_at <= now))
            .order_by(Notification.created_at).limit(limit)))
    deliver_outbox(Outbox(mids, nids), session_factory)
    return len(mids) + len(nids)


def recover_stale_sends(session_factory=SessionLocal) -> int:
    """Rows stuck in 'sending' (the process died during the HTTP call): outcome unknown, so flag, don't resend."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.outbox_sending_timeout_seconds)
    unknown = SendResult(ok=False, wa_message_id=None, delivery_status="failed",
                         error="Delivery outcome unknown (worker stopped while sending); not re-sent to avoid a duplicate")
    with session_factory() as db:
        stale = list(db.scalars(select(Message).where(Message.delivery_status == "sending",
                                                       Message.send_started_at < cutoff).with_for_update(skip_locked=True)))
        for msg in stale:
            msg.send_attempts = settings.outbox_max_attempts  # never retried
            _record_message_result(msg, ConversationRepo(db, msg.business_id).get(msg.conversation_id), unknown)
        stale_n = list(db.scalars(select(Notification).where(Notification.status == "sending",
                                                             Notification.send_started_at < cutoff)
                                  .with_for_update(skip_locked=True)))
        for n in stale_n:
            n.status, n.error = "failed", unknown.error
        db.commit()
        return len(stale) + len(stale_n)
