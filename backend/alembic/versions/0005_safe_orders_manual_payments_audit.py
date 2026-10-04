"""safe orders, manual payments, audit trail, owner notifications

- orders: fulfilment status and payment_status are separated
  (pending -> accepted -> ready -> out_for_delivery -> delivered | cancelled) x (unpaid | pending | paid);
  confirmation evidence (confirmation_message_id, confirmed_at), owner decision timestamps and reason.
- payments: manual (owner-confirmed) payments with method, external reference, who confirmed, note.
- carts.checkout: the server-prepared summary awaiting the customer's explicit YES.
- business_settings: payment instructions and owner notification channel.
- audit_events (append-only, enforced by trigger) and notifications (owner outbox).

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def _tenant_fk_trigger(table: str, refs: list[tuple[str, str]]) -> None:
    cols = ", ".join(c for c, _ in refs)
    args = ", ".join(f"'{c}', '{p}'" for c, p in refs)
    op.execute(f"DROP TRIGGER IF EXISTS tenant_fk_{table} ON {table}")
    op.execute(f"CREATE TRIGGER tenant_fk_{table} BEFORE INSERT OR UPDATE OF business_id, {cols} ON {table} "
               f"FOR EACH ROW EXECUTE FUNCTION duka_enforce_same_tenant({args})")


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("recipient", sa.String(length=32), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.String(length=30), nullable=True),
        sa.Column("entity_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_send_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("send_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("wa_message_id", sa.String(length=128), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("business_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_notifications_business_id"), "notifications", ["business_id"], unique=False)
    op.create_index("ix_notifications_outbox", "notifications", ["next_send_at"], unique=False,
                    postgresql_where=sa.text("status IN ('queued', 'retry')"))
    op.create_table(
        "audit_events",
        sa.Column("actor_type", sa.String(length=10), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.String(length=60), nullable=False),
        sa.Column("entity_type", sa.String(length=30), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=True),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"),
                  nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("business_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_events_business_id"), "audit_events", ["business_id"], unique=False)
    op.create_index(op.f("ix_audit_events_entity_id"), "audit_events", ["entity_id"], unique=False)

    op.add_column("business_settings", sa.Column("payment_instructions", sa.Text(), nullable=True))
    op.add_column("business_settings", sa.Column("owner_notification_phone", sa.String(length=32), nullable=True))
    op.add_column("business_settings", sa.Column("owner_notification_template", sa.String(length=100), nullable=True))
    op.add_column("business_settings", sa.Column("owner_notification_template_language", sa.String(length=10),
                                                 server_default="en", nullable=False))
    op.add_column("carts", sa.Column("checkout", postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    op.add_column("orders", sa.Column("payment_status", sa.String(length=12), server_default="unpaid", nullable=False))
    op.add_column("orders", sa.Column("confirmation_message_id", sa.UUID(), nullable=True))
    op.add_column("orders", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("cancel_reason", sa.Text(), nullable=True))
    op.create_foreign_key("orders_confirmation_message_id_fkey", "orders", "messages", ["confirmation_message_id"],
                          ["id"], ondelete="SET NULL")

    op.add_column("payments", sa.Column("method", sa.String(length=20), nullable=True))
    op.add_column("payments", sa.Column("external_reference", sa.String(length=128), nullable=True))
    op.add_column("payments", sa.Column("confirmation_source", sa.String(length=10), nullable=True))
    op.add_column("payments", sa.Column("confirmed_by_user_id", sa.UUID(), nullable=True))
    op.add_column("payments", sa.Column("note", sa.Text(), nullable=True))
    op.create_index("uq_payments_external_reference", "payments", ["business_id", "external_reference"], unique=True,
                    postgresql_where=sa.text("status = 'successful' AND external_reference IS NOT NULL"))
    op.create_foreign_key("payments_confirmed_by_user_id_fkey", "payments", "users", ["confirmed_by_user_id"],
                          ["id"], ondelete="SET NULL")

    # Existing data: split the old combined status into fulfilment + payment.
    op.execute("UPDATE orders SET payment_status = 'paid' WHERE status = 'paid' OR paid_at IS NOT NULL")
    op.execute("UPDATE orders SET payment_status = 'pending' WHERE status = 'awaiting_payment'")
    op.execute("UPDATE orders SET status = 'accepted', accepted_at = updated_at WHERE status IN ('paid', 'processing')")
    op.execute("UPDATE orders SET status = 'pending' WHERE status = 'awaiting_payment'")
    op.execute("UPDATE payments SET confirmation_source = 'provider' WHERE status = 'successful'")

    # Tenant integrity for the new references (see 0003) and immutability for the new tables.
    _tenant_fk_trigger("orders", [("customer_id", "customers"), ("conversation_id", "conversations"),
                                  ("confirmation_message_id", "messages")])
    _tenant_fk_trigger("payments", [("order_id", "orders"), ("confirmed_by_user_id", "users")])
    _tenant_fk_trigger("audit_events", [("actor_user_id", "users")])
    for table in ("audit_events", "notifications"):
        op.execute(f"CREATE TRIGGER tenant_immutable_{table} BEFORE UPDATE OF business_id ON {table} "
                   f"FOR EACH ROW EXECUTE FUNCTION duka_business_id_immutable()")
    op.execute("""
    CREATE OR REPLACE FUNCTION duka_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'check_violation';
    END $$;
    """)
    op.execute("CREATE TRIGGER audit_events_append_only BEFORE UPDATE ON audit_events "
               "FOR EACH ROW EXECUTE FUNCTION duka_append_only()")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS duka_append_only()")
    _tenant_fk_trigger("orders", [("customer_id", "customers"), ("conversation_id", "conversations")])
    _tenant_fk_trigger("payments", [("order_id", "orders")])
    op.execute("UPDATE orders SET status = 'paid' WHERE status = 'accepted' AND payment_status = 'paid'")
    op.execute("UPDATE orders SET status = 'processing' WHERE status = 'accepted'")
    op.execute("UPDATE orders SET status = 'awaiting_payment' WHERE status = 'pending' AND payment_status = 'pending'")
    op.drop_constraint("payments_confirmed_by_user_id_fkey", "payments", type_="foreignkey")
    op.drop_index("uq_payments_external_reference", table_name="payments")
    for col in ("note", "confirmed_by_user_id", "confirmation_source", "external_reference", "method"):
        op.drop_column("payments", col)
    op.drop_constraint("orders_confirmation_message_id_fkey", "orders", type_="foreignkey")
    for col in ("cancel_reason", "cancelled_at", "accepted_at", "confirmed_at", "confirmation_message_id",
                "payment_status"):
        op.drop_column("orders", col)
    op.drop_column("carts", "checkout")
    for col in ("owner_notification_template_language", "owner_notification_template", "owner_notification_phone",
                "payment_instructions"):
        op.drop_column("business_settings", col)
    op.drop_index(op.f("ix_audit_events_entity_id"), table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_business_id"), table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_notifications_outbox", table_name="notifications")
    op.drop_index(op.f("ix_notifications_business_id"), table_name="notifications")
    op.drop_table("notifications")
