"""usage ledger (usage_events)

One row per metered event, written in its own transaction right after the external call it records (so far: real
AI model calls), so usage survives a rolled-back turn. Durable by design: the only foreign key is business_id ->
businesses ON DELETE RESTRICT, so deleting customers or conversations or purging webhook_events never removes usage,
and no personal data is stored. Idempotent: unique (business_id, idempotency_key). Insert-only: UPDATE and DELETE are
both rejected by duka_append_only() (the function audit_events uses for UPDATE, 0005), since the ledger is the record
of usage and cost; business_id is immutable like every tenant table (0003).

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_events",
        sa.Column("business_id", sa.UUID(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"),
                  nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("source_type", sa.String(length=30), nullable=True),
        sa.Column("source_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("units", sa.Integer(), server_default="1", nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("tool_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("configured_model", sa.String(length=100), nullable=True),
        sa.Column("cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("price_version", sa.String(length=40), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.CheckConstraint("kind IN ('llm_call')", name="ck_usage_events_kind"),
        sa.CheckConstraint("units >= 0 AND tool_calls >= 0 AND attempts >= 0 AND input_tokens >= 0 "
                           "AND output_tokens >= 0 AND cost_micros >= 0", name="ck_usage_events_non_negative"),
        sa.CheckConstraint("(cost_micros IS NULL) = (currency IS NULL) "
                           "AND (cost_micros IS NULL OR price_version IS NOT NULL)", name="ck_usage_events_cost"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "idempotency_key", name="uq_usage_events_idempotency"),
    )
    op.create_index("ix_usage_events_business_occurred", "usage_events", ["business_id", "occurred_at"], unique=False)
    op.create_index("ix_usage_events_occurred", "usage_events", ["occurred_at"], unique=False)
    op.execute("CREATE TRIGGER tenant_immutable_usage_events BEFORE UPDATE OF business_id ON usage_events "
               "FOR EACH ROW EXECUTE FUNCTION duka_business_id_immutable()")
    op.execute("CREATE TRIGGER usage_events_append_only BEFORE UPDATE OR DELETE ON usage_events "
               "FOR EACH ROW EXECUTE FUNCTION duka_append_only()")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS usage_events_append_only ON usage_events")
    op.execute("DROP TRIGGER IF EXISTS tenant_immutable_usage_events ON usage_events")
    op.drop_index("ix_usage_events_occurred", table_name="usage_events")
    op.drop_index("ix_usage_events_business_occurred", table_name="usage_events")
    op.drop_table("usage_events")
