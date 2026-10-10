import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin


class UsageEvent(IdMixin, Base):
    """Usage ledger: one row per metered event, the source of truth for what a tenant used and cost — never
    agent_runs, whose rows roll back with a failed turn and are deleted with the conversation. Kinds: llm_call (a
    real AI model call), wa_in (an inbound customer WhatsApp message), wa_out (an attempt to send a message to a
    customer) and wa_alert (an attempt to send an owner alert).

    An external call is recorded in its own short transaction right after it (services/usage_service.py), so it
    survives the turn's rollback, deleted customers/conversations and purged webhooks; wa_in and late failures are
    recorded in the transaction of the state they describe. No foreign key except to businesses, which is RESTRICT
    (TenantMixin would CASCADE), and source_id is a plain uuid. No personal data (market is a country calling code,
    never a number). Insert-only: a trigger rejects UPDATE and DELETE (duka_append_only), so a correction is a new
    event (a late failure), never an edit. Unpriced (no matching price) = cost_micros IS NULL."""

    __tablename__ = "usage_events"
    __table_args__ = (
        UniqueConstraint("business_id", "idempotency_key", name="uq_usage_events_idempotency"),
        Index("ix_usage_events_business_occurred", "business_id", "occurred_at"),
        Index("ix_usage_events_occurred", "occurred_at"),
        CheckConstraint("kind IN ('llm_call', 'wa_in', 'wa_out', 'wa_alert')", name="ck_usage_events_kind"),
        CheckConstraint("units >= 0 AND tool_calls >= 0 AND attempts >= 0 AND input_tokens >= 0 "
                        "AND output_tokens >= 0 AND cost_micros >= 0", name="ck_usage_events_non_negative"),
        CheckConstraint("(cost_micros IS NULL) = (currency IS NULL) AND (cost_micros IS NULL OR price_version IS NOT NULL)",
                        name="ck_usage_events_cost"),
        CheckConstraint("(kind = 'llm_call' AND is_real IS NULL AND message_kind IS NULL AND template_name IS NULL "
                        "AND market IS NULL) OR (kind <> 'llm_call' AND (is_real IS NOT NULL OR status = 'unknown'))",
                        name="ck_usage_events_wa_fields"),
        CheckConstraint("kind = 'llm_call' OR (kind = 'wa_in' AND status = 'received') OR (kind IN ('wa_out', "
                        "'wa_alert') AND status IN ('success', 'failed', 'unknown', 'late_failed'))",
                        name="ck_usage_events_wa_status"),
        CheckConstraint("message_kind IS NULL OR message_kind IN ('free_form', 'template')",
                        name="ck_usage_events_message_kind"),
        CheckConstraint("template_name IS NULL OR (message_kind IS NOT NULL AND message_kind = 'template')", name="ck_usage_events_template"),
        CheckConstraint("market IS NULL OR market ~ '^[1-9][0-9]{0,2}$'", name="ck_usage_events_market"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="RESTRICT"), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp(),
                                                  nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # llm_call | wa_in | wa_out | wa_alert
    # Unique per tenant: a second write of the same event is a no-op (llm_call: "llm:<uuid made before the call>";
    # wa_in: "wa_in:<wamid>"; wa_out/wa_alert: "<kind>:<message or notification id>:<send attempt>"; the late failure
    # of one: "wa_late_fail:<message or notification id>").
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    # What the usage was for (llm_call: agent_run = a reply turn, conversation = its summary; WhatsApp: message |
    # notification); no foreign key.
    source_type: Mapped[str | None] = mapped_column(String(30))
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # llm_call: success | error. wa_in: received. wa_out/wa_alert: success | failed | unknown (a send interrupted, its
    # outcome not known) | late_failed (WhatsApp reported a message it had accepted as failed; units 0, not a send).
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    units: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)  # as reported by the provider; NULL = not reported
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # llm_call: provider attempts. WhatsApp: the outbox send attempt (Message.send_attempts / Notification.attempts);
    # one attempt may hold several HTTP requests inside the Cloud adapter, which are not counted here.
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    provider: Mapped[str | None] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(100))  # served, as the provider reports it
    configured_model: Mapped[str | None] = mapped_column(String(100))  # the model Duka asked for
    # Estimated cost when written, in millionths of `currency`, from the price list `price_version`
    # (USAGE_PRICING_FILE); prices changed later never rewrite it.
    cost_micros: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str | None] = mapped_column(String(3))
    price_version: Mapped[str | None] = mapped_column(String(40))
    # WhatsApp only (NULL for llm_call). is_real: a real WhatsApp message (a send by the Cloud API; an inbound message
    # whose webhook signature was verified) vs a development/simulated one. message_kind: free_form | template for
    # sends, NULL when not known. is_real is NULL only for an unknown outcome whose realness cannot be told. template_name: only for a template. market: the recipient's country calling code
    # (e.g. "250"), NULL when it cannot be resolved; never a phone number.
    is_real: Mapped[bool | None] = mapped_column(Boolean)
    message_kind: Mapped[str | None] = mapped_column(String(20))
    template_name: Mapped[str | None] = mapped_column(String(100))
    market: Mapped[str | None] = mapped_column(String(3))


class AiUsageCounter(IdMixin, TenantMixin, Base):
    """Operational counters of the Runaway Conversation Guard (docs/P1_RUNAWAY_GUARD.md, services/ai_guard.py): how
    many model calls and provider HTTP attempts were RESERVED, before being made, per inbound message (`message`: the
    webhook event, across retries; period `lifetime`), per customer and per tenant (`hour` / `day`: fixed UTC
    buckets). Mutable and short-lived, unlike the insert-only usage_events ledger, which stays the history of what
    actually happened; old buckets are purged (ops.purge_ai_usage_counters).

    A reservation is made in its own short transaction before the call, so it survives a rollback of the turn (the
    spend was real) and only ever over-counts (a crash between reservation and call). `over_limit` counts reservations
    that went past a configured limit in observe mode; `denied` counts reservations refused in enforce mode;
    `alerted_at` marks the owner alert sent for a tenant bucket (at most one per bucket)."""

    __tablename__ = "ai_usage_counters"
    __table_args__ = (
        UniqueConstraint("business_id", "scope", "subject_id", "period", "period_start", name="uq_ai_usage_counters_key"),
        Index("ix_ai_usage_counters_period_start", "period_start"),
        CheckConstraint("scope IN ('message', 'customer', 'tenant')", name="ck_ai_usage_counters_scope"),
        CheckConstraint("period IN ('lifetime', 'hour', 'day')", name="ck_ai_usage_counters_period"),
        CheckConstraint("calls >= 0 AND attempts >= 0 AND over_limit >= 0 AND denied >= 0",
                        name="ck_ai_usage_counters_non_negative"),
    )

    scope: Mapped[str] = mapped_column(String(10), nullable=False)
    # The webhook event (message), the customer, or the business itself (tenant): never NULL, so the key is unique.
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    period: Mapped[str] = mapped_column(String(10), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    calls: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    over_limit: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    denied: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
