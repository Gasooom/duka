"""Runaway Conversation Guard counters (ai_usage_counters)

Operational counters for docs/P1_RUNAWAY_GUARD.md: model calls and provider HTTP attempts reserved before they are
made, per inbound message (the webhook event, across retries), per customer and per tenant, in fixed UTC hour/day
buckets. Mutable and purged after a while; the insert-only usage_events ledger is not touched. business_id is
immutable like every tenant table (0003); deleting a business deletes its counters (operational data only).

Revision ID: 0011
Revises: 0010
"""
import sqlalchemy as sa

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_usage_counters",
        sa.Column("scope", sa.String(length=10), nullable=False),
        sa.Column("subject_id", sa.UUID(), nullable=False),
        sa.Column("period", sa.String(length=10), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("over_limit", sa.Integer(), server_default="0", nullable=False),
        sa.Column("denied", sa.Integer(), server_default="0", nullable=False),
        sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("business_id", sa.UUID(), nullable=False),
        sa.CheckConstraint("scope IN ('message', 'customer', 'tenant')", name="ck_ai_usage_counters_scope"),
        sa.CheckConstraint("period IN ('lifetime', 'hour', 'day')", name="ck_ai_usage_counters_period"),
        sa.CheckConstraint("calls >= 0 AND attempts >= 0 AND over_limit >= 0 AND denied >= 0",
                           name="ck_ai_usage_counters_non_negative"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "scope", "subject_id", "period", "period_start",
                            name="uq_ai_usage_counters_key"),
    )
    op.create_index(op.f("ix_ai_usage_counters_business_id"), "ai_usage_counters", ["business_id"], unique=False)
    op.create_index("ix_ai_usage_counters_period_start", "ai_usage_counters", ["period_start"], unique=False)
    op.execute("CREATE TRIGGER tenant_immutable_ai_usage_counters BEFORE UPDATE OF business_id ON ai_usage_counters "
               "FOR EACH ROW EXECUTE FUNCTION duka_business_id_immutable()")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS tenant_immutable_ai_usage_counters ON ai_usage_counters")
    op.drop_index("ix_ai_usage_counters_period_start", table_name="ai_usage_counters")
    op.drop_index(op.f("ix_ai_usage_counters_business_id"), table_name="ai_usage_counters")
    op.drop_table("ai_usage_counters")
