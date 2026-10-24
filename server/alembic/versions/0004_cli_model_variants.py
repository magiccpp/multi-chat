"""variants of an agent-CLI model family (Cursor: effort / thinking / long-context slugs)

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("models", sa.Column("cli_variants", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("models", "cli_variants")
