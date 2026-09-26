"""Create users table

Revision ID: 3fa5003b63e3
Revises:
Create Date: 2026-09-26 14:20:27.367878

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3fa5003b63e3"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "users",
        sa.Column("id", sa.BIGINT(), primary_key=True),
        sa.Column("amount", sa.INTEGER(), nullable=False, server_default="100"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("users")
