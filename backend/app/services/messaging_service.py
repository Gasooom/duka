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
from app.integrations.whatsapp.adapters import META_WINDOW_ERROR, OUTSIDE_WINDOW, SendResult, get_adapter
from app.models import Conversation, Message, Notification, WhatsAppAccount
from app.repositories.repos import ConversationRepo, NotificationRepo, SettingsRepo, WhatsAppAccountRepo
from app.services.conversation_service import ConversationService
from app.services.usage_service import WA_ALERT, WA_OUT, record_wa_late_failure, record_wa_send

logger = get_logger(__name__)
OUTBOX_KEY = "duka_outbox"
NOTIFY_KEY = "duka_notifications"
RETRY_DELAYS_SECONDS = (10, 60, 300, 900)
WINDOW_ALERT = "whatsapp_window_closed"      # owner alert kinds (notifications.kind)
FAILED_ALERT = "whatsapp_delivery_failed"


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


@dataclass(frozen=True)
class SendMeter:
    """What a send attempt is recorded as in the usage ledger: WA_OUT for a message to a customer, WA_ALERT for an
    owner alert, and the outbox row (a message or a notification) it belongs to."""
    kind: str
    source_type: str
    source_id: uuid.UUID


def _send(db: Session, business_id: uuid.UUID, to: str, body: str, attempt: int, *,
          template: tuple[str, str] | None = None, meter: SendMeter | None = None) -> SendResult:
    """One send attempt (`attempt` = the number the atomic claim handed out). With a `meter`, an attempt that reached
    an adapter that sends is recorded in the usage ledger, in its own transaction, whatever its outcome. Nothing is
    recorded when nothing was attempted: no active account, or an adapter that cannot send or is not metered (a
    24-hour window found closed never gets here at all)."""
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
        if result.http_attempts is not None:
            ctx["http_attempts"] = result.http_attempts
    if meter is not None and adapter.metered:
        record_wa_send(db.get_bind(), business_id, kind=meter.kind, source_type=meter.source_type,
                       source_id=meter.source_id, attempt=attempt, status="success" if result.ok else "failed",
                       is_real=adapter.is_real, message_kind="template" if template else "free_form",
                       template_name=template[0] if template else None, recipient=to)
    return result


def _next_state(result: SendResult, attempts: int) -> tuple[str, datetime | None]:
    if result.ok:
        return result.delivery_status, None
    if result.retryable and attempts < settings.outbox_max_attempts:
        delay = RETRY_DELAYS_SECONDS[min(attempts - 1, len(RETRY_DELAYS_SECONDS) - 1)]
        return "retry", datetime.now(timezone.utc) + timedelta(seconds=delay)
    return "failed", None


# ---------------------------------------------------------------- customer messages
def _outside_window(last_inbound: datetime | None) -> bool:
    """WhatsApp delivers normal messages only within 24 h of the customer's last message; after that only approved
    templates. A customer who never wrote is outside it too."""
    return last_inbound is None or \
        datetime.now(timezone.utc) - last_inbound > timedelta(hours=settings.whatsapp_window_hours)


def _seen(last_inbound: datetime | None) -> str:
    if last_inbound is None:
        return "has never written to your WhatsApp number"
    hours = (datetime.now(timezone.utc) - last_inbound).total_seconds() / 3600
    return f"last wrote {hours:.0f} hours ago"


def _who(conv: Conversation) -> str:
    return f"{conv.customer.name or 'Customer'} (+{conv.customer.whatsapp_number})"


def _quote(msg: Message) -> str:
    text = " ".join((msg.content or "").split())
    return text[:280] + ("…" if len(text) > 280 else "")


def _alert_owner_once(db: Session, business_id: uuid.UUID, conv: Conversation, kind: str, body: str) -> None:
    """One owner alert per conversation and kind until the customer writes again: every new customer message
    reopens the window, so a later failure is news again."""
    since = ConversationService(db, business_id).last_inbound_at(conv.customer_id)
    where = [Notification.kind == kind, Notification.entity_type == "conversation", Notification.entity_id == conv.id]
    if since is not None:
        where.append(Notification.created_at > since)
    if NotificationRepo(db, business_id).first(*where) is None:
        notify_owner(db, business_id, kind, body, entity_type="conversation", entity_id=conv.id)


def _window_alert(conv: Conversation, msg: Message, last_inbound: datetime | None, *, by_meta: bool = False) -> str:
    why = ("WhatsApp reports that more than 24 hours had passed since the customer's last message" if by_meta else
           f"the customer {_seen(last_inbound)}, and WhatsApp only delivers normal messages within 24 hours of the "
           "customer's last message")
    return (f"⚠️ WhatsApp message not delivered to {_who(conv)}: {why}. Contact them another way (call or SMS), or "
            f"wait until they write again.\nNot delivered: “{_quote(msg)}”")


def _deliver_one(message_id: uuid.UUID, session_factory) -> None:
    db = session_factory()
    outbox = None
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
        last_inbound = ConversationService(db, business_id).last_inbound_at(conv.customer_id)
        closed = _outside_window(last_inbound)
        if closed:
            # Never attempted: WhatsApp would refuse it (or accept it and drop it later).
            result = SendResult(ok=False, wa_message_id=None, delivery_status="failed", reason=OUTSIDE_WINDOW,
                                error=f"Not sent: the customer {_seen(last_inbound)}; outside WhatsApp's 24-hour "
                                      "window only approved template messages can be delivered")
        else:
            result = _send(db, business_id, conv.customer.whatsapp_number, msg.content, msg.send_attempts,
                           meter=SendMeter(WA_OUT, "message", msg.id))
        if not result.ok and result.reason == OUTSIDE_WINDOW:
            conv = ConversationRepo(db, business_id).get(conv.id, for_update=True)  # one alert at a time
            _alert_owner_once(db, business_id, conv, WINDOW_ALERT,
                              _window_alert(conv, msg, last_inbound, by_meta=not closed))
        _record_message_result(msg, conv, result)
        outbox = take_outbox(db)
        db.commit()
    finally:
        db.close()
        clear_context()
    if outbox is not None:
        deliver_outbox(outbox, session_factory)  # the owner alert, if one was queued


def record_late_failure(db: Session, business_id: uuid.UUID, msg: Message, error_code: int | None,
                        error_title: str | None, *, verified: bool = False) -> None:
    """Meta accepted a message, then reported it failed (status webhook). The owner must not believe the customer
    got it: record why, flag the conversation and alert the owner once. The caller commits; the alert is sent by
    the outbox like any other. The usage ledger gets one late-failure event (units 0), in this same transaction;
    the success event of the attempt stays as it was. `verified`: the status webhook's signature was verified."""
    window = error_code == META_WINDOW_ERROR
    detail = f"error {error_code}: {error_title or 'no details'}" if error_code else (error_title or "no details")
    attrs = dict(msg.attributes or {})
    attrs.update(error=f"WhatsApp reported this message as not delivered ({detail})",
                 failure_reason=OUTSIDE_WINDOW if window else "whatsapp_failed")
    if error_code:
        attrs["error_code"] = error_code
    msg.attributes, msg.delivery_status = attrs, "failed"
    conv = ConversationRepo(db, business_id).get(msg.conversation_id)
    record_wa_late_failure(db, business_id, kind=WA_OUT, source_type="message", source_id=msg.id,
                           attempt=msg.send_attempts, verified=verified, message_kind="free_form",
                           recipient=conv.customer.whatsapp_number if conv else None)
    if conv is None:
        return
    conv.needs_attention = True
    log_event(logger, "outbox.failed_late", 40, operation="outbox.status", status="error", error=detail)
    if window:
        _alert_owner_once(db, business_id, conv, WINDOW_ALERT, _window_alert(conv, msg, None, by_meta=True))
    else:
        _alert_owner_once(db, business_id, conv, FAILED_ALERT,
                          f"⚠️ WhatsApp could not deliver a message to {_who(conv)} ({detail}). Open the conversation "
                          f"in the Duka dashboard.\nNot delivered: “{_quote(msg)}”")


def record_late_notification_failure(db: Session, business_id: uuid.UUID, n: Notification, error_code: int | None,
                                     error_title: str | None, *, verified: bool = False) -> None:
    """An owner alert that Meta accepted and then failed: shown as failed, with the reason, in the dashboard, and
    one late-failure event (units 0) in the usage ledger, in the caller's transaction."""
    record_wa_late_failure(db, business_id, kind=WA_ALERT, source_type="notification", source_id=n.id,
                           attempt=n.attempts, verified=verified, message_kind=None, recipient=n.recipient)
    detail = f"error {error_code}: {error_title or 'no details'}" if error_code else (error_title or "no details")
    hint = (" You have not written to your shop's WhatsApp number in 24 hours: set an approved owner-notification "
            "template in Settings." if error_code == META_WINDOW_ERROR else "")
    n.status, n.error = "failed", f"WhatsApp reported this alert as not delivered ({detail}).{hint}"


def _record_message_result(msg: Message, conv: Conversation | None, result: SendResult) -> None:
    attrs = dict(msg.attributes or {})
    msg.delivery_status, msg.next_send_at = _next_state(result, msg.send_attempts)
    if result.ok:
        msg.wa_message_id = result.wa_message_id
        for key in ("error", "error_code", "failure_reason"):
            attrs.pop(key, None)
    else:
        attrs["error"] = result.error
        if result.reason:
            attrs["failure_reason"] = result.reason
        if result.error_code:
            attrs["error_code"] = result.error_code
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
        result = _send(db, business_id, n.recipient, n.body, n.attempts, template=template,
                       meter=SendMeter(WA_ALERT, "notification", n.id))
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
            conv = ConversationRepo(db, msg.business_id).get(msg.conversation_id)
            # Before the attempt counter below is overwritten; written before the commit, so a crash in between
            # leaves the row 'sending' and the next sweep records the same key again (a no-op).
            _record_interrupted_send(db, msg.business_id, WA_OUT, "message", msg.id, msg.send_attempts, "free_form",
                                     conv.customer.whatsapp_number if conv else None, msg.send_started_at)
            msg.send_attempts = settings.outbox_max_attempts  # never retried
            _record_message_result(msg, conv, unknown)
        stale_n = list(db.scalars(select(Notification).where(Notification.status == "sending",
                                                             Notification.send_started_at < cutoff)
                                  .with_for_update(skip_locked=True)))
        for n in stale_n:
            # What was sent (template or text) is not known any more: the settings may have changed since.
            _record_interrupted_send(db, n.business_id, WA_ALERT, "notification", n.id, n.attempts, None, n.recipient,
                                     n.send_started_at)
            n.status, n.error = "failed", unknown.error
        db.commit()
        return len(stale) + len(stale_n)


def _record_interrupted_send(db: Session, business_id: uuid.UUID, kind: str, source_type: str, source_id: uuid.UUID,
                             attempt: int, message_kind: str | None, recipient: str | None,
                             claimed_at: datetime | None) -> None:
    """A claimed send that never reported back: its outcome is unknown. Recorded under the attempt's own key, so a
    result the attempt did manage to write is kept as it is. Whether the attempt was a real send is not stored
    anywhere: it is read from the account only if the account has not been changed since the attempt was claimed
    (`claimed_at`), otherwise it is left NULL (not known) rather than described by today's configuration. Nothing is
    recorded when nothing can have been sent (no active account, an adapter that sends nothing)."""
    try:
        account = _active_account(db, business_id)
        if account is None:
            return
        adapter = get_adapter(account)
        client = getattr(adapter, "client", None)  # the Cloud adapter opens an HTTP client we never use here
        if client is not None:
            client.close()
        if adapter.metered:
            unchanged = claimed_at is not None and account.updated_at <= claimed_at
            record_wa_send(db.get_bind(), business_id, kind=kind, source_type=source_type, source_id=source_id,
                           attempt=attempt, status="unknown", is_real=adapter.is_real if unchanged else None,
                           message_kind=message_kind, template_name=None, recipient=recipient)
    except Exception as exc:  # recovery of the row itself must go on
        log_event(logger, "usage.record_failed", 40, operation="usage", status="error", error=repr(exc)[:300],
                  business_id=str(business_id), source_id=str(source_id), kind=kind, event_status="unknown")
