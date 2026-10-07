"""Customers, conversations, messages and agent-run records."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.errors import ValidationError
from app.models import AgentRun, Conversation, Customer, Message
from app.repositories.repos import AgentRunRepo, ConversationRepo, CustomerRepo, MessageRepo


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if not 7 <= len(digits) <= 15:
        raise ValidationError(f"Invalid phone number '{raw}'")
    return digits


class CustomerService:
    def __init__(self, db: Session, business_id: uuid.UUID):
        self.db = db
        self.business_id = business_id
        self.repo = CustomerRepo(db, business_id)

    def upsert_from_whatsapp(self, whatsapp_number: str, name: str | None = None) -> Customer:
        number = normalize_phone(whatsapp_number)
        stmt = (
            pg_insert(Customer)
            .values(id=uuid.uuid4(), business_id=self.business_id, whatsapp_number=number, name=name, attributes={})
            .on_conflict_do_nothing(index_elements=["business_id", "whatsapp_number"])
        )
        self.db.execute(stmt)
        customer = self.repo.first(Customer.whatsapp_number == number)
        if name and not customer.name:
            customer.name = name
        self.db.flush()
        return customer

    def list(self, *, q: str | None = None, limit: int = 200) -> list[Customer]:
        where = []
        if q:
            where.append((Customer.whatsapp_number.ilike(f"%{q}%")) | (Customer.name.ilike(f"%{q}%")))
        return self.repo.list(where=where, order_by=[Customer.created_at.desc()], limit=limit)

    def get(self, customer_id: uuid.UUID) -> Customer:
        return self.repo.get_or_404(customer_id)


class ConversationService:
    def __init__(self, db: Session, business_id: uuid.UUID):
        self.db = db
        self.business_id = business_id
        self.repo = ConversationRepo(db, business_id)
        self.messages = MessageRepo(db, business_id)
        self.runs = AgentRunRepo(db, business_id)

    def get_or_create_active(self, customer: Customer) -> Conversation:
        where = (Conversation.customer_id == customer.id, Conversation.status != "closed")
        conv = self.repo.first(*where)
        if conv is None:
            # Race-safe: the partial unique index uq_conversations_open allows one open conversation per customer.
            self.db.execute(pg_insert(Conversation).values(
                id=uuid.uuid4(), business_id=self.business_id, customer_id=customer.id, status="ai",
                needs_attention=False, summarized_message_count=0, state={},
            ).on_conflict_do_nothing(index_elements=["business_id", "customer_id"],
                                     index_where=text("status <> 'closed'")))
            conv = self.repo.first(*where)
        return conv

    def get(self, conversation_id: uuid.UUID, *, for_update: bool = False) -> Conversation:
        return self.repo.get_or_404(conversation_id, for_update=for_update)

    def list(self, *, status: str | None = None, needs_attention: bool | None = None, limit: int = 100) -> list[Conversation]:
        where = []
        if status:
            where.append(Conversation.status == status)
        if needs_attention is not None:
            where.append(Conversation.needs_attention.is_(needs_attention))
        return self.repo.list(where=where, order_by=[Conversation.last_message_at.desc().nullslast()], limit=limit)

    def record_inbound(self, conv: Conversation, *, text: str, wa_message_id: str | None,
                       metadata: dict[str, Any] | None = None) -> Message | None:
        """Insert an inbound customer message idempotently. Returns None for duplicates
        (Meta retries webhooks; the unique (business_id, wa_message_id) key de-duplicates)."""
        msg_id = uuid.uuid4()
        stmt = (
            pg_insert(Message)
            .values(id=msg_id, business_id=self.business_id, conversation_id=conv.id, role="customer",
                    content=text, attributes=metadata or {}, wa_message_id=wa_message_id, delivery_status="received")
            .on_conflict_do_nothing(constraint="uq_messages_business_wa_id")
            .returning(Message.id)
        )
        inserted = self.db.execute(stmt).scalar()
        if inserted is None:
            return None
        conv.last_message_at = datetime.now(timezone.utc)
        self.db.flush()
        return self.messages.get(inserted)

    def add_message(self, conv: Conversation, role: str, content: str, *, metadata: dict[str, Any] | None = None,
                    delivery_status: str | None = None, wa_message_id: str | None = None,
                    agent_run_id: uuid.UUID | None = None) -> Message:
        msg = self.messages.add(conversation_id=conv.id, role=role, content=content, attributes=metadata or {},
                                delivery_status=delivery_status, wa_message_id=wa_message_id,
                                agent_run_id=agent_run_id)
        conv.last_message_at = datetime.now(timezone.utc)
        return msg

    def last_inbound_at(self, customer_id: uuid.UUID) -> datetime | None:
        """When the customer last wrote to the shop (any of their conversations): WhatsApp's 24-hour customer-service
        window counts from this. Our own messages never extend it (conversation.last_message_at includes them), and
        neither do WhatsApp system notices about the customer."""
        return self.db.scalar(
            select(func.max(Message.created_at))
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Message.business_id == self.business_id, Conversation.customer_id == customer_id,
                   Message.role == "customer",
                   func.coalesce(Message.attributes["type"].astext, "text") != "system"))

    def history(self, conv: Conversation, *, limit: int | None = None, roles: tuple[str, ...] | None = None) -> list[Message]:
        where = [Message.conversation_id == conv.id]
        if roles:
            where.append(Message.role.in_(roles))
        msgs = self.messages.list(where=where, order_by=[Message.created_at.desc()], limit=limit)
        return list(reversed(msgs))

    def message_count(self, conv: Conversation, roles: tuple[str, ...] = ("customer", "assistant", "human_agent")) -> int:
        return self.messages.count(Message.conversation_id == conv.id, Message.role.in_(roles))

    def runs_for(self, conv: Conversation, limit: int = 50) -> list[AgentRun]:
        return self.runs.list(where=[AgentRun.conversation_id == conv.id], order_by=[AgentRun.created_at.desc()],
                              limit=limit)

    def update_language(self, conv: Conversation, message: Message, text: str, ignore_terms: set[str]) -> str | None:
        """Detect the language of an inbound text and update the conversation's language state (only on a
        confident detection; see app/agents/language.py). The detection is kept on the message for debugging."""
        from app.agents.language import detect, next_language
        det = detect(text, ignore_terms)
        message.attributes = {**(message.attributes or {}), "language": {
            "detected": det.code, "confidence": det.confidence, "dialect_confidence": det.dialect_confidence,
            "evidence": det.evidence}}
        new, confirmed = next_language(conv.language_code, det)
        if new != conv.language_code or confirmed:
            conv.language_code = new
            conv.language_confidence = det.confidence
            conv.language_updated_at = datetime.now(timezone.utc)
        return conv.language_code

    def set_state(self, conv: Conversation, **updates: Any) -> None:
        conv.state = {**(conv.state or {}), **updates}  # reassign so JSONB change is detected

    def handoff(self, conv: Conversation, reason: str | None) -> None:
        conv.status = "human"
        conv.needs_attention = True
        conv.handoff_reason = reason
        self.add_message(conv, "system", f"Conversation handed off to a human. Reason: {reason or 'n/a'}")

    def return_to_ai(self, conv: Conversation) -> None:
        conv.status = "ai"
        conv.needs_attention = False
        conv.handoff_reason = None
        self.add_message(conv, "system", "Conversation returned to AI mode")

    def attention_count(self) -> int:
        return self.repo.count(Conversation.needs_attention.is_(True))

    def message_total(self) -> int:
        return self.messages.count(Message.role.in_(("customer", "assistant", "human_agent")))

    def latest_messages_by_conversation(self, conv_ids: list[uuid.UUID]) -> dict[uuid.UUID, Message]:
        if not conv_ids:
            return {}
        sub = (self.messages.select(Message.conversation_id, func.max(Message.created_at).label("mx"))
               .where(Message.conversation_id.in_(conv_ids), Message.role.in_(("customer", "assistant", "human_agent")))
               .group_by(Message.conversation_id).subquery())
        stmt = self.messages.query().join(sub, (Message.conversation_id == sub.c.conversation_id)
                                          & (Message.created_at == sub.c.mx))
        return {m.conversation_id: m for m in self.db.scalars(stmt).all()}
