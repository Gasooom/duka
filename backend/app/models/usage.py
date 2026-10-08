import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin


class UsageEvent(IdMixin, Base):
    """Usage ledger: one row per metered event (so far: each real AI model call), the source of truth for what a
    tenant used and cost — never agent_runs, whose rows roll back with a failed turn and are deleted with the
    conversation.

    Written in its own short transaction right after the external call (services/usage_service.py), so it survives
    the turn's rollback, deleted customers/conversations and purged webhooks: no foreign key except to businesses,
    which is RESTRICT (TenantMixin would CASCADE), and source_id is a plain uuid. No personal data. Insert-only:
    a trigger rejects UPDATE and DELETE (duka_append_only). Unpriced (no matching price) = cost_micros IS NULL."""

    __tablename__ = "usage_events"
    __table_args__ = (
        UniqueConstraint("business_id", "idempotency_key", name="uq_usage_events_idempotency"),
        Index("ix_usage_events_business_occurred", "business_id", "occurred_at"),
        Index("ix_usage_events_occurred", "occurred_at"),
        CheckConstraint("kind IN ('llm_call')", name="ck_usage_events_kind"),
        CheckConstraint("units >= 0 AND tool_calls >= 0 AND attempts >= 0 AND input_tokens >= 0 "
                        "AND output_tokens >= 0 AND cost_micros >= 0", name="ck_usage_events_non_negative"),
        CheckConstraint("(cost_micros IS NULL) = (currency IS NULL) AND (cost_micros IS NULL OR price_version IS NOT NULL)",
                        name="ck_usage_events_cost"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="RESTRICT"), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp(),
                                                  nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # llm_call
    # Unique per tenant: a second write of the same event is a no-op (llm_call: "llm:<uuid made before the call>").
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    # What the usage was for (llm_call: agent_run = a reply turn, conversation = its summary); no foreign key.
    source_type: Mapped[str | None] = mapped_column(String(30))
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # llm_call: success | error
    units: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)  # as reported by the provider; NULL = not reported
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    provider: Mapped[str | None] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(100))  # served, as the provider reports it
    configured_model: Mapped[str | None] = mapped_column(String(100))  # the model Duka asked for
    # Estimated cost when written, in millionths of `currency`, from the price list `price_version`
    # (USAGE_PRICING_FILE); prices changed later never rewrite it.
    cost_micros: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str | None] = mapped_column(String(3))
    price_version: Mapped[str | None] = mapped_column(String(40))
