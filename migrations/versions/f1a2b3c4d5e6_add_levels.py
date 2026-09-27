"""Add level tables (levels / level VC sessions)

Revision ID: f1a2b3c4d5e6
Revises: e7a3c9f2b1d4
Create Date: 2026-09-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "e7a3c9f2b1d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "levels",
        sa.Column("user_id", sa.BIGINT(), primary_key=True),
        sa.Column("xp", sa.INTEGER(), nullable=False, server_default="0"),
        sa.Column("level", sa.INTEGER(), nullable=False, server_default="1"),
        sa.Column("messages", sa.INTEGER(), nullable=False, server_default="0"),
        sa.Column("vc_minutes", sa.INTEGER(), nullable=False, server_default="0"),
        sa.Column("last_chat_at", sa.TEXT(), nullable=True),
        sa.Column("last_content", sa.TEXT(), nullable=True),
        sa.Column("updated_at", sa.TEXT(), nullable=False),
    )
    op.create_table(
        "level_vc_sessions",
        sa.Column("user_id", sa.BIGINT(), primary_key=True),
        sa.Column("join_at", sa.TEXT(), nullable=False),
        sa.Column("last_credit", sa.TEXT(), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("level_vc_sessions")
    op.drop_table("levels")
