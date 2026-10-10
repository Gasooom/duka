"""WhatsApp usage in the usage ledger (usage_events)

Extends the one ledger instead of adding a second table: the kinds wa_in (an inbound customer message), wa_out (a
send attempt to a customer, or the late failure Meta reports for one) and wa_alert (the same for an owner alert),
next to llm_call. New nullable columns: is_real (a real WhatsApp message vs a development/simulated one; required for
the WhatsApp kinds, NULL for llm_call and for a send whose outcome is unknown when whether it was real cannot be
told), message_kind (free_form | template), template_name (only for a template) and
market (the recipient's country calling code, never a phone number: 1-3 digits is enforced).

Existing rows are untouched (the ledger is insert-only; nothing is backfilled). Downgrade refuses while WhatsApp
rows exist: they could only be removed by defeating the append-only trigger.

Revision ID: 0010
Revises: 0009
"""
import sqlalchemy as sa

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_LLM = "kind = 'llm_call'"


def upgrade() -> None:
    op.add_column("usage_events", sa.Column("is_real", sa.Boolean(), nullable=True))
    op.add_column("usage_events", sa.Column("message_kind", sa.String(length=20), nullable=True))
    op.add_column("usage_events", sa.Column("template_name", sa.String(length=100), nullable=True))
    op.add_column("usage_events", sa.Column("market", sa.String(length=3), nullable=True))
    op.drop_constraint("ck_usage_events_kind", "usage_events", type_="check")
    op.create_check_constraint("ck_usage_events_kind", "usage_events",
                               "kind IN ('llm_call', 'wa_in', 'wa_out', 'wa_alert')")
    op.create_check_constraint(
        "ck_usage_events_wa_fields", "usage_events",
        f"({_LLM} AND is_real IS NULL AND message_kind IS NULL AND template_name IS NULL AND market IS NULL) "
        "OR (kind <> 'llm_call' AND (is_real IS NOT NULL OR status = 'unknown'))")
    op.create_check_constraint(
        "ck_usage_events_wa_status", "usage_events",
        f"{_LLM} OR (kind = 'wa_in' AND status = 'received') "
        "OR (kind IN ('wa_out', 'wa_alert') AND status IN ('success', 'failed', 'unknown', 'late_failed'))")
    op.create_check_constraint("ck_usage_events_message_kind", "usage_events",
                               "message_kind IS NULL OR message_kind IN ('free_form', 'template')")
    op.create_check_constraint("ck_usage_events_template", "usage_events",
                               "template_name IS NULL OR (message_kind IS NOT NULL AND message_kind = 'template')")
    op.create_check_constraint("ck_usage_events_market", "usage_events",
                               "market IS NULL OR market ~ '^[1-9][0-9]{0,2}$'")


def downgrade() -> None:
    conn = op.get_bind()
    if conn.execute(sa.text("SELECT EXISTS (SELECT 1 FROM usage_events WHERE kind <> 'llm_call')")).scalar():
        raise RuntimeError(
            "Cannot downgrade 0010: usage_events holds WhatsApp usage (wa_in/wa_out/wa_alert). The ledger is "
            "insert-only, so those rows cannot be removed; keep this migration.")
    for name in ("ck_usage_events_market", "ck_usage_events_template", "ck_usage_events_message_kind",
                 "ck_usage_events_wa_status", "ck_usage_events_wa_fields"):
        op.drop_constraint(name, "usage_events", type_="check")
    op.drop_constraint("ck_usage_events_kind", "usage_events", type_="check")
    op.create_check_constraint("ck_usage_events_kind", "usage_events", "kind IN ('llm_call')")
    for column in ("market", "template_name", "message_kind", "is_real"):
        op.drop_column("usage_events", column)
