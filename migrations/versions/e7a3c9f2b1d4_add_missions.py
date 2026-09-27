"""Add mission tables (progress / VC sessions / config)

Revision ID: e7a3c9f2b1d4
Revises: b8e3f05d2c7a
Create Date: 2026-09-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7a3c9f2b1d4"
down_revision: str | Sequence[str] | None = "b8e3f05d2c7a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "mission_progress",
        sa.Column("user_id", sa.BIGINT(), nullable=False),
        sa.Column("mission_id", sa.TEXT(), nullable=False),
        sa.Column("period_key", sa.TEXT(), nullable=False),
        sa.Column("progress", sa.INTEGER(), nullable=False, server_default="0"),
        sa.Column("completed", sa.INTEGER(), nullable=False, server_default="0"),
        sa.Column("claimed", sa.INTEGER(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.TEXT(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "mission_id", "period_key"),
    )
    op.create_table(
        "vc_sessions",
        sa.Column("user_id", sa.BIGINT(), primary_key=True),
        sa.Column("join_at", sa.TEXT(), nullable=False),
        sa.Column("last_credit", sa.TEXT(), nullable=False),
    )
    op.create_table(
        "mission_config",
        sa.Column("key", sa.TEXT(), primary_key=True),
        sa.Column("value", sa.TEXT(), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("mission_config")
    op.drop_table("vc_sessions")
    op.drop_table("mission_progress")
