"""Add ai_chat tables (histories / personas / global)

Revision ID: a1b2c3d4e5f6
Revises: e3f5a7c9d1b2
Create Date: 2026-09-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "e3f5a7c9d1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "ai_histories",
        sa.Column("id", sa.INTEGER(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BIGINT(), nullable=False),
        sa.Column("role", sa.TEXT(), nullable=False),
        sa.Column("content", sa.TEXT(), nullable=False),
        sa.Column("created_at", sa.TEXT(), nullable=False),
    )
    op.create_index("ix_ai_histories_user", "ai_histories", ["user_id", "id"])
    op.create_table(
        "ai_personas",
        sa.Column("user_id", sa.BIGINT(), primary_key=True),
        sa.Column("system_prompt", sa.TEXT(), nullable=False),
        sa.Column("updated_at", sa.TEXT(), nullable=False),
    )
    op.create_table(
        "ai_global",
        sa.Column("key", sa.TEXT(), primary_key=True),
        sa.Column("value", sa.TEXT(), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("ai_global")
    op.drop_table("ai_personas")
    op.drop_index("ix_ai_histories_user", table_name="ai_histories")
    op.drop_table("ai_histories")
