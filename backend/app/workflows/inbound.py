"""WhatsApp inbound pipeline:

  webhook payload -> parse -> resolve tenant by phone_number_id -> upsert customer ->
  conversation -> idempotent message insert -> (human mode? stop) -> agent -> send reply

Each message is processed in its own transaction so one failure never affects others,
and the webhook endpoint itself never raises because of an agent/LLM failure."""
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.engine import AgentEngine
from app.core.logging import bind_context, clear_context, get_logger, log_event, log_operation
from app.core.ratelimit import inbound_message_limiter
from app.db.session import SessionLocal
from app.integrations.whatsapp.parser import InboundMessage, parse_webhook
from app.models import Business, Message, WhatsAppAccount
from app.services.conversation_service import ConversationService, CustomerService
from app.services.messaging_service import send_to_customer

logger = get_logger(__name__)


@dataclass
class ProcessResult:
    status: str  # replied | duplicate | unknown_tenant | human_mode | rate_limited | unsupported | error
    business_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    reply: str | None = None
    agent_run_id: uuid.UUID | None = None
    replies: list[str] = field(default_factory=list)


def resolve_account(db: Session, phone_number_id: str) -> WhatsAppAccount | None:
    return db.scalar(select(WhatsAppAccount).where(WhatsAppAccount.phone_number_id == phone_number_id,
                                                   WhatsAppAccount.is_active.is_(True)))


def process_message(db: Session, msg: InboundMessage) -> ProcessResult:
    account = resolve_account(db, msg.phone_number_id)
    if account is None:
        # Never process a message without a tenant.
        log_event(logger, "inbound.unknown_tenant", 30, operation="inbound", status="dropped",
                  phone_number_id=msg.phone_number_id)
        return ProcessResult(status="unknown_tenant")
    business = db.get(Business, account.business_id)
    if business is None or not business.is_active:
        return ProcessResult(status="unknown_tenant")
    bind_context(business_id=business.id)

    customer = CustomerService(db, business.id).upsert_from_whatsapp(msg.from_number, msg.profile_name)
    convs = ConversationService(db, business.id)
    conv = convs.get_or_create_active(customer)
    bind_context(customer_id=customer.id, conversation_id=conv.id)
    text = (msg.text or "").strip()
    inbound = convs.record_inbound(conv, text=text or f"[{msg.type} message]", wa_message_id=msg.wa_message_id,
                                   metadata={"type": msg.type})
    if inbound is None:
        log_event(logger, "inbound.duplicate", operation="inbound", status="duplicate")
        return ProcessResult(status="duplicate", business_id=business.id, conversation_id=conv.id)
    # Lock the conversation row so concurrent messages from one customer are handled in order.
    conv = convs.get(conv.id, for_update=True)

    if conv.status == "human":
        conv.needs_attention = True
        return ProcessResult(status="human_mode", business_id=business.id, conversation_id=conv.id)

    if not inbound_message_limiter.allow(f"{business.id}:{customer.whatsapp_number}"):
        return ProcessResult(status="rate_limited", business_id=business.id, conversation_id=conv.id)

    if not text:
        reply = "Sorry, I can only read text messages for now. Please type your request."
        send_to_customer(db, business.id, conv, reply, account=account)
        return ProcessResult(status="unsupported", business_id=business.id, conversation_id=conv.id, reply=reply)

    outcome = AgentEngine(db, business).run(customer, conv, inbound)
    send_to_customer(db, business.id, conv, outcome.text, agent_run_id=outcome.run.id, account=account)
    return ProcessResult(status="replied", business_id=business.id, conversation_id=conv.id, reply=outcome.text,
                         agent_run_id=outcome.run.id)


def process_webhook_payload(payload: dict, session_factory=SessionLocal) -> list[ProcessResult]:
    messages, statuses = parse_webhook(payload)
    results: list[ProcessResult] = []
    for msg in messages:
        db = session_factory()
        try:
            with log_operation(logger, "inbound.process", wa_message_id=msg.wa_message_id) as ctx:
                result = process_message(db, msg)
                db.commit()
                ctx["result"] = result.status
            results.append(result)
        except Exception as exc:
            db.rollback()
            log_event(logger, "inbound.failed", 40, operation="inbound.process", status="error", error=repr(exc)[:300])
            results.append(ProcessResult(status="error"))
        finally:
            db.close()
            clear_context()
    if statuses:
        _apply_statuses(statuses, session_factory)
    return results


def _apply_statuses(statuses, session_factory) -> None:
    db = session_factory()
    try:
        for s in statuses:
            account = resolve_account(db, s.phone_number_id)
            if not account:
                continue
            msg = db.scalar(select(Message).where(Message.business_id == account.business_id,
                                                  Message.wa_message_id == s.wa_message_id))
            if msg:
                msg.delivery_status = s.status
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
