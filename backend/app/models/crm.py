import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class Customer(IdMixin, TimestampMixin, TenantMixin, Base):
    """Customers are per-business: the same WhatsApp number may exist under several tenants."""

    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("business_id", "whatsapp_number"),)

    whatsapp_number: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(255))
    attributes: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, nullable=False)


class Conversation(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "conversations"
    # At most one open conversation per customer, even under concurrent first messages.
    __table_args__ = (Index("uq_conversations_open", "business_id", "customer_id", unique=True,
                            postgresql_where=text("status <> 'closed'")),)

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(10), default="ai", nullable=False)  # ai | human | closed
    needs_attention: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    handoff_reason: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    summarized_message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Small deterministic working memory, e.g. the last product list shown to the customer.
    state: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    customer: Mapped["Customer"] = relationship(lazy="joined")


class Message(IdMixin, TenantMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("business_id", "wa_message_id", name="uq_messages_business_wa_id"),
        Index("ix_messages_outbox", "next_send_at", postgresql_where=text("delivery_status IN ('queued', 'retry')")),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # customer | assistant | tool_call | tool_result | system | human_agent
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    attributes: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, nullable=False)
    wa_message_id: Mapped[str | None] = mapped_column(String(128))
    # inbound: received. outbound (outbox): queued -> sending -> sent|simulated, or retry -> ... -> failed.
    # Meta status webhooks then advance sent -> delivered -> read (or failed).
    delivery_status: Mapped[str | None] = mapped_column(String(20))
    send_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    next_send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    send_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # clock_timestamp(), not now(): now() is the transaction start, so every message written in one
    # webhook transaction would tie and the conversation order would be undefined.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp(),
                                                 nullable=False)


class AgentRun(IdMixin, TenantMixin, Base):
    """One agent invocation: every LLM decision, tool call, tool result, error, latency, tokens."""

    __tablename__ = "agent_runs"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"))
    trigger_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # success | error | fast_path
    input_text: Mapped[str | None] = mapped_column(Text)
    steps: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    response_text: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # clock_timestamp(), not now(): now() is the transaction start, so every message written in one
    # webhook transaction would tie and the conversation order would be undefined.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp(),
                                                 nullable=False)


class WebhookEvent(IdMixin, TimestampMixin, TenantMixin, Base):
    """Durable inbox: one row per inbound WhatsApp message, written before the webhook is acknowledged.

    Workers claim rows (FOR UPDATE SKIP LOCKED) in `seq` order, one at a time per sender, under a lease so a
    crashed worker's event is picked up again. status: pending | processing | retry | done | dead."""

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("business_id", "provider", "external_id", name="uq_webhook_events_external"),
        Index("ix_webhook_events_due", "next_attempt_at",
              postgresql_where=text("status IN ('pending', 'retry', 'processing')")),
        Index("ix_webhook_events_sender", "business_id", "sender", "seq"),
    )

    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(String(20), default="whatsapp", nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)  # wamid
    sender: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                      nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(String(20))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
