"""use clock_timestamp() for message/agent_run ordering

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("messages", "agent_runs"):
        op.execute(f"ALTER TABLE {table} ALTER COLUMN created_at SET DEFAULT clock_timestamp()")


def downgrade() -> None:
    for table in ("messages", "agent_runs"):
        op.execute(f"ALTER TABLE {table} ALTER COLUMN created_at SET DEFAULT now()")
