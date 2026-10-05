"""WhatsApp inbound pipeline, durable:

  POST /webhooks/whatsapp -> verify signature -> ingest(): one webhook_events row per message, keyed by
  (business, wamid), committed BEFORE the 200 (a DB failure returns 5xx so Meta retries)
  -> worker claims events (FOR UPDATE SKIP LOCKED, oldest first, one at a time per sender, under a lease)
  -> process_message(): customer -> conversation -> idempotent message insert -> agent -> reply queued in the
     outbox, all in ONE transaction that also marks the event done
  -> after commit: the reply is sent (services/messaging_service.py).

Crash before commit: nothing is persisted except the event, whose lease expires and which is processed again.
Failure: the event goes to 'retry' with backoff, then 'dead' after WEBHOOK_MAX_ATTEMPTS (logged at ERROR).
Webhooks never crash on LLM/tool failure: the agent returns the tenant fallback message instead."""
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.agents.engine import AgentEngine
from app.core.config import settings
from app.core.errors import ValidationError
from app.core.logging import bind_context, clear_context, get_logger, log_event, log_operation, safe_error
from app.core.ratelimit import inbound_message_limiter
from app.db.session import SessionLocal
from app.i18n import media_label, t
from app.integrations.whatsapp.parser import InboundMessage, StatusUpdate, parse_webhook
from app.models import Business, Message, Product, ProductCategory, WebhookEvent, WhatsAppAccount
from app.repositories.repos import SettingsRepo
from app.services.commerce_service import CheckoutService
from app.services.conversation_service import ConversationService, CustomerService, normalize_phone
from app.services.messaging_service import deliver_outbox, notify_owner, send_to_customer, take_outbox
from app.workflows.handoff import ai_paused_reply, conversation_language, handoff_reply, request_human

logger = get_logger(__name__)
IGNORED_TYPES = {"reaction", "system", "ephemeral"}


def language_ignore_terms(db: Session, business: Business, customer) -> set[str]:
    """Words that say nothing about the customer's language: the catalog's product and category names, the shop's
    and the customer's names."""
    names = set(db.scalars(select(Product.name).where(Product.business_id == business.id)))
    names |= set(db.scalars(select(ProductCategory.name).where(ProductCategory.business_id == business.id)))
    names |= {business.name, customer.name or ""}
    terms = {n.lower() for n in names if n}
    return terms | {w for n in terms for w in n.split() if len(w) >= 4}
RETRY_DELAYS_SECONDS = (2, 10, 30, 120)
# Meta status webhooks can arrive out of order; never move a message backwards (read -> delivered).
_STATUS_RANK = {"queued": 0, "sending": 1, "retry": 1, "sent": 2, "simulated": 2, "delivered": 3, "read": 4}


@dataclass
class ProcessResult:
    # replied | duplicate | unknown_tenant | human_mode | rate_limited | unsupported | queued | error
    status: str
    business_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    reply: str | None = None
    agent_run_id: uuid.UUID | None = None
    replies: list[str] = field(default_factory=list)


@dataclass
class IngestResult:
    # one entry per inbound message in the payload: (message, event id or None, outcome)
    # outcome: accepted | duplicate | unknown_tenant | invalid
    items: list[tuple[InboundMessage, uuid.UUID | None, str]] = field(default_factory=list)

    @property
    def event_ids(self) -> list[uuid.UUID]:
        return [eid for _, eid, outcome in self.items if outcome == "accepted"]


def resolve_account(db: Session, phone_number_id: str) -> WhatsAppAccount | None:
    return db.scalar(select(WhatsAppAccount).where(WhatsAppAccount.phone_number_id == phone_number_id,
                                                   WhatsAppAccount.is_active.is_(True)))


# ---------------------------------------------------------------- ingest (inside the webhook request)
def ingest(db: Session, payload: dict) -> IngestResult:
    """Persist every message of a webhook payload as a webhook_events row (idempotent) and apply status
    updates. The caller commits; only then may the webhook be acknowledged."""
    messages, statuses = parse_webhook(payload)
    out = IngestResult()
    for msg in messages:
        account = resolve_account(db, msg.phone_number_id)
        business = db.get(Business, account.business_id) if account else None
        if business is None or not business.is_active:
            # Never process (or store) a message without a tenant.
            log_event(logger, "inbound.unknown_tenant", 30, operation="inbound.ingest", status="dropped",
                      phone_number_id=msg.phone_number_id)
            out.items.append((msg, None, "unknown_tenant"))
            continue
        try:
            sender = normalize_phone(msg.from_number)
        except ValidationError:
            sender = ""
        if not sender or not msg.wa_message_id:
            log_event(logger, "inbound.invalid", 30, operation="inbound.ingest", status="dropped",
                      business_id=str(business.id))
            out.items.append((msg, None, "invalid"))
            continue
        event_id = db.execute(
            pg_insert(WebhookEvent).values(
                id=uuid.uuid4(), business_id=business.id, provider="whatsapp", external_id=msg.wa_message_id,
                sender=sender, payload=msg.to_payload(), status="pending", attempts=0)
            .on_conflict_do_nothing(constraint="uq_webhook_events_external").returning(WebhookEvent.id)).scalar()
        out.items.append((msg, event_id, "accepted" if event_id else "duplicate"))
        if event_id is None:
            log_event(logger, "inbound.duplicate", operation="inbound.ingest", status="duplicate",
                      business_id=str(business.id))
    _apply_statuses(db, statuses)
    return out


def ingest_and_commit(payload: dict, session_factory=SessionLocal) -> IngestResult:
    db = session_factory()
    try:
        result = ingest(db, payload)
        db.commit()
        return result
    finally:
        db.close()


def _apply_statuses(db: Session, statuses: list[StatusUpdate]) -> None:
    for s in statuses:
        account = resolve_account(db, s.phone_number_id)
        if not account:
            continue
        # Scoped by the receiving number's tenant: a status can only touch that tenant's messages.
        msg = db.scalar(select(Message).where(Message.business_id == account.business_id,
                                              Message.wa_message_id == s.wa_message_id))
        if msg is None or msg.delivery_status == s.status:
            continue
        if s.status == "failed" or _STATUS_RANK.get(s.status, -1) > _STATUS_RANK.get(msg.delivery_status or "", -1):
            msg.delivery_status = s.status


# ---------------------------------------------------------------- claim + process (worker)
_CLAIM_SQL = text("""
UPDATE webhook_events
   SET status = 'processing', attempts = attempts + 1, updated_at = now(),
       locked_until = now() + make_interval(secs => :lease)
 WHERE id = (
       SELECT e.id FROM webhook_events e
        WHERE ((e.status IN ('pending', 'retry') AND e.next_attempt_at <= now())
               OR (e.status = 'processing' AND e.locked_until < now()))
          AND (CAST(:only_id AS uuid) IS NULL OR e.id = CAST(:only_id AS uuid))
          -- per-sender FIFO: never overtake an unfinished earlier message from the same customer
          AND NOT EXISTS (SELECT 1 FROM webhook_events p
                           WHERE p.business_id = e.business_id AND p.sender = e.sender AND p.seq < e.seq
                             AND p.status IN ('pending', 'retry', 'processing'))
        ORDER BY e.seq
        LIMIT 1
          FOR UPDATE SKIP LOCKED)
RETURNING id, attempts
""")


def claim(session_factory=SessionLocal, only_id: uuid.UUID | None = None) -> tuple[uuid.UUID, int] | None:
    with session_factory() as db:
        row = db.execute(_CLAIM_SQL, {"lease": settings.webhook_lease_seconds,
                                      "only_id": str(only_id) if only_id else None}).first()
        db.commit()
        return (row[0], row[1]) if row else None


def process_event(event_id: uuid.UUID, attempts: int, session_factory=SessionLocal) -> ProcessResult:
    if attempts > settings.webhook_max_attempts:  # e.g. a message that kills the process every time
        _record_failure(session_factory, event_id, attempts, "Exceeded max attempts (lease expired repeatedly)")
        return ProcessResult(status="error")
    db = session_factory()
    outbox = None
    try:
        event = db.get(WebhookEvent, event_id)
        msg = InboundMessage.from_payload(event.payload)
        with log_operation(logger, "inbound.process", attempt=attempts) as ctx:
            result = process_message(db, msg)
            ctx["result"] = result.status
        event.status, event.result = "done", result.status
        event.processed_at, event.locked_until, event.last_error = datetime.now(timezone.utc), None, None
        outbox = take_outbox(db)
        db.commit()  # inbound message, agent run, cart/order changes, queued reply and 'done' — atomically
    except Exception as exc:
        db.rollback()
        take_outbox(db)
        _record_failure(session_factory, event_id, attempts, safe_error(exc, 1000))
        return ProcessResult(status="error")
    finally:
        db.close()
        clear_context()
    deliver_outbox(outbox, session_factory)
    return result


def _record_failure(session_factory, event_id: uuid.UUID, attempts: int, error: str) -> None:
    with session_factory() as db:
        event = db.get(WebhookEvent, event_id)
        if event is None or event.status == "done":
            return
        event.last_error = error[:1000]
        event.locked_until = None
        if attempts >= settings.webhook_max_attempts:
            event.status = "dead"
            log_event(logger, "webhook.dead", 40, operation="inbound.process", status="dead",
                      business_id=str(event.business_id), attempts=attempts, error=error[:300])
        else:
            event.status = "retry"
            delay = RETRY_DELAYS_SECONDS[min(attempts - 1, len(RETRY_DELAYS_SECONDS) - 1)]
            event.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            log_event(logger, "webhook.retry", 30, operation="inbound.process", status="retry",
                      business_id=str(event.business_id), attempts=attempts, error=error[:300])
        db.commit()


def run_due(session_factory=SessionLocal, limit: int | None = None) -> list[ProcessResult]:
    """Process due events until none is claimable (or `limit` reached)."""
    results = []
    while limit is None or len(results) < limit:
        claimed = claim(session_factory)
        if claimed is None:
            break
        results.append(process_event(*claimed, session_factory=session_factory))
    return results


def process_message(db: Session, msg: InboundMessage) -> ProcessResult:
    account = resolve_account(db, msg.phone_number_id)
    if account is None:
        return ProcessResult(status="unknown_tenant")
    business = db.get(Business, account.business_id)
    if business is None or not business.is_active:
        return ProcessResult(status="unknown_tenant")
    bind_context(business_id=business.id)

    customer = CustomerService(db, business.id).upsert_from_whatsapp(msg.from_number, msg.profile_name)
    convs = ConversationService(db, business.id)
    conv = convs.get_or_create_active(customer)
    bind_context(customer_id=customer.id, conversation_id=conv.id)
    text_ = (msg.text or "").strip()
    inbound = convs.record_inbound(conv, text=text_ or f"[{msg.type} message]", wa_message_id=msg.wa_message_id,
                                   metadata={"type": msg.type})
    if inbound is None:
        log_event(logger, "inbound.duplicate", operation="inbound", status="duplicate")
        return ProcessResult(status="duplicate", business_id=business.id, conversation_id=conv.id)
    # Lock the conversation row so concurrent messages from one customer are handled in order.
    conv = convs.get(conv.id, for_update=True)
    if text_:  # before any routing, so even paused/human conversations show the customer's current language
        convs.update_language(conv, inbound, text_, language_ignore_terms(db, business, customer))
    lang = conversation_language(conv, business)

    if conv.status == "human":
        conv.needs_attention = True
        return ProcessResult(status="human_mode", business_id=business.id, conversation_id=conv.id)

    shop_settings = SettingsRepo(db, business.id).first()
    if shop_settings is not None and not shop_settings.ai_enabled:
        # Assistant paused by the owner: nothing automated happens (no answers, no orders). The customer gets
        # one acknowledgement per waiting conversation and the owner one alert.
        if not conv.needs_attention:
            conv.needs_attention = True
            send_to_customer(db, business.id, conv, ai_paused_reply(business, lang), metadata={"event": "ai_paused"})
            notify_owner(db, business.id, "message_waiting",
                         f"💬 New WhatsApp message from {customer.name or '+' + customer.whatsapp_number} while the "
                         f"assistant is paused: {text_[:200] or '[' + msg.type + ']'}",
                         entity_type="conversation", entity_id=conv.id)
        return ProcessResult(status="ai_paused", business_id=business.id, conversation_id=conv.id)

    if not inbound_message_limiter.allow(f"{business.id}:{customer.whatsapp_number}"):
        return ProcessResult(status="rate_limited", business_id=business.id, conversation_id=conv.id)

    if not text_:
        if msg.type in IGNORED_TYPES:  # reactions etc.: nothing to answer
            return ProcessResult(status="ignored", business_id=business.id, conversation_id=conv.id)
        if business.human_handoff_enabled:
            # No speech-to-text / vision in the MVP: never guess what a voice note or photo says.
            request_human(db, business, conv, f"Customer sent {media_label(msg.type, 'en')}")
            reply = t("media_unsupported", lang, label=media_label(msg.type, lang)) + \
                handoff_reply(business, lang, message=True)
        else:
            reply = t("text_only", lang)
        send_to_customer(db, business.id, conv, reply)
        return ProcessResult(status="unsupported", business_id=business.id, conversation_id=conv.id, reply=reply)

    outcome = AgentEngine(db, business).run(customer, conv, inbound)
    sent = send_to_customer(db, business.id, conv, outcome.text, agent_run_id=outcome.run.id)
    if outcome.checkout_cart_id:
        CheckoutService(db, business.id).attach_summary_message(outcome.checkout_cart_id, sent.id)
    return ProcessResult(status="replied", business_id=business.id, conversation_id=conv.id, reply=outcome.text,
                         agent_run_id=outcome.run.id)


def process_webhook_payload(payload: dict, session_factory=SessionLocal) -> list[ProcessResult]:
    """Ingest a payload and process its events right away (dev simulator, tests). Production goes through
    ingest_and_commit() in the webhook + the worker; both share every step after ingest."""
    ingested = ingest_and_commit(payload, session_factory)
    results: list[ProcessResult] = []
    for _, event_id, outcome in ingested.items:
        if outcome != "accepted":
            results.append(ProcessResult(status=outcome))
            continue
        claimed = claim(session_factory, only_id=event_id)
        # Not claimable now (an earlier message from this customer is still pending): the worker will do it.
        results.append(process_event(*claimed, session_factory=session_factory) if claimed
                       else ProcessResult(status="queued"))
    return results
