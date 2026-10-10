"""Embeddings requests in the usage ledger (usage_events)

Adds the kind `embedding`: one event per request to a paid embeddings provider (units = the texts in the request,
input_tokens as the provider reports them, attempts = the HTTP attempts of that request), next to llm_call. Like
llm_call it carries no WhatsApp fields and the WhatsApp status rules do not apply to it (success | error are
written). No new column.

Existing rows are untouched (the ledger is insert-only; nothing is backfilled). Downgrade refuses while embedding
rows exist: they could only be removed by defeating the append-only trigger.

Revision ID: 0012
Revises: 0011
"""
import sqlalchemy as sa

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def _replace_checks(kinds: str, ai: str, not_ai: str) -> None:
    """ck_usage_events_kind (`kinds`) and the two WhatsApp checks, whose rules do not apply to the AI kinds (`ai`)."""
    for name in ("ck_usage_events_wa_status", "ck_usage_events_wa_fields", "ck_usage_events_kind"):
        op.drop_constraint(name, "usage_events", type_="check")
    op.create_check_constraint("ck_usage_events_kind", "usage_events", kinds)
    op.create_check_constraint(
        "ck_usage_events_wa_fields", "usage_events",
        f"({ai} AND is_real IS NULL AND message_kind IS NULL AND template_name IS NULL AND market IS NULL) "
        f"OR ({not_ai} AND (is_real IS NOT NULL OR status = 'unknown'))")
    op.create_check_constraint(
        "ck_usage_events_wa_status", "usage_events",
        f"{ai} OR (kind = 'wa_in' AND status = 'received') "
        "OR (kind IN ('wa_out', 'wa_alert') AND status IN ('success', 'failed', 'unknown', 'late_failed'))")


def upgrade() -> None:
    _replace_checks("kind IN ('llm_call', 'embedding', 'wa_in', 'wa_out', 'wa_alert')",
                    "kind IN ('llm_call', 'embedding')", "kind NOT IN ('llm_call', 'embedding')")


def downgrade() -> None:
    conn = op.get_bind()
    if conn.execute(sa.text("SELECT EXISTS (SELECT 1 FROM usage_events WHERE kind = 'embedding')")).scalar():
        raise RuntimeError(
            "Cannot downgrade 0012: usage_events holds embeddings usage (kind embedding). The ledger is insert-only, "
            "so those rows cannot be removed; keep this migration.")
    _replace_checks("kind IN ('llm_call', 'wa_in', 'wa_out', 'wa_alert')", "kind = 'llm_call'", "kind <> 'llm_call'")
