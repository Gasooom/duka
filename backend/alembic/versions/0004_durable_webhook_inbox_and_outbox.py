"""durable webhook inbox and outbox

- webhook_events: inbound WhatsApp messages are persisted here before the webhook is acknowledged and
  processed by workers (lease + retry + dead-letter), replacing in-memory BackgroundTasks.
- messages.send_attempts / next_send_at / send_started_at: outbound messages are queued in the same
  transaction as the state they describe and sent only after commit.
- at most one open conversation and one active cart per customer.

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table, where in (("conversations", "status <> 'closed'"), ("carts", "status = 'active'")):
        dupes = bind.execute(sa.text(f"SELECT count(*) FROM (SELECT 1 FROM {table} WHERE {where} "
                                     "GROUP BY business_id, customer_id HAVING count(*) > 1) d")).scalar()
        if dupes:
            raise RuntimeError(f"{dupes} customers have several rows in {table} WHERE {where}; merge them first")

    op.create_table(
        "webhook_events",
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("sender", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("result", sa.String(length=20), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("business_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "provider", "external_id", name="uq_webhook_events_external"),
        sa.UniqueConstraint("seq"),
    )
    op.create_index(op.f("ix_webhook_events_business_id"), "webhook_events", ["business_id"], unique=False)
    op.create_index("ix_webhook_events_due", "webhook_events", ["next_attempt_at"], unique=False,
                    postgresql_where=sa.text("status IN ('pending', 'retry', 'processing')"))
    op.create_index("ix_webhook_events_sender", "webhook_events", ["business_id", "sender", "seq"], unique=False)
    op.execute("CREATE TRIGGER tenant_immutable_webhook_events BEFORE UPDATE OF business_id ON webhook_events "
               "FOR EACH ROW EXECUTE FUNCTION duka_business_id_immutable()")

    op.create_index("uq_carts_active", "carts", ["business_id", "customer_id"], unique=True,
                    postgresql_where=sa.text("status = 'active'"))
    op.create_index("uq_conversations_open", "conversations", ["business_id", "customer_id"], unique=True,
                    postgresql_where=sa.text("status <> 'closed'"))

    op.add_column("messages", sa.Column("send_attempts", sa.Integer(), server_default="0", nullable=False))
    op.add_column("messages", sa.Column("next_send_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("messages", sa.Column("send_started_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_messages_outbox", "messages", ["next_send_at"], unique=False,
                    postgresql_where=sa.text("delivery_status IN ('queued', 'retry')"))


def downgrade() -> None:
    op.drop_index("ix_messages_outbox", table_name="messages")
    op.drop_column("messages", "send_started_at")
    op.drop_column("messages", "next_send_at")
    op.drop_column("messages", "send_attempts")
    op.drop_index("uq_conversations_open", table_name="conversations")
    op.drop_index("uq_carts_active", table_name="carts")
    op.drop_index("ix_webhook_events_sender", table_name="webhook_events")
    op.drop_index("ix_webhook_events_due", table_name="webhook_events")
    op.drop_index(op.f("ix_webhook_events_business_id"), table_name="webhook_events")
    op.drop_table("webhook_events")
