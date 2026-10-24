"""context window and effort per session model; model context length and effort support; compactions

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("models", sa.Column("context_length", sa.Integer(), nullable=True))
    op.add_column("models", sa.Column("supports_effort", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("session_models", sa.Column("context_window", sa.Integer(), nullable=True))
    op.add_column("session_models", sa.Column("effort", sa.String(length=16), nullable=True))
    op.create_table(
        "compactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("model_id", sa.Integer(), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=False),
        sa.Column("before_message_id", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("tokens_before", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_compactions_session_id", "compactions", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_compactions_session_id", table_name="compactions")
    op.drop_table("compactions")
    op.drop_column("session_models", "effort")
    op.drop_column("session_models", "context_window")
    op.drop_column("models", "supports_effort")
    op.drop_column("models", "context_length")
