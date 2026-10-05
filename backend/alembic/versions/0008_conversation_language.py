"""conversation language state

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("language_code", sa.String(length=8), nullable=True))
    op.add_column("conversations", sa.Column("language_confidence", sa.Float(), nullable=True))
    op.add_column("conversations", sa.Column("language_updated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("conversations", "language_updated_at")
    op.drop_column("conversations", "language_confidence")
    op.drop_column("conversations", "language_code")
