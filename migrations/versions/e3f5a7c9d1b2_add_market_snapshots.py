"""Add market_snapshots (currency value index source)

Revision ID: e3f5a7c9d1b2
Revises: d2e4f6a8b0c1
Create Date: 2026-09-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e3f5a7c9d1b2"
down_revision: str | Sequence[str] | None = "d2e4f6a8b0c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "market_snapshots",
        sa.Column("id", sa.INTEGER(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.TEXT(), nullable=False),
        sa.Column("avg_price", sa.REAL(), nullable=False),
        sa.Column("total_supply", sa.INTEGER(), nullable=False),
    )
    # 起点となる1行を現状からシード (指数100の基準点)
    op.execute(
        sa.text(
            "INSERT INTO market_snapshots (created_at, avg_price, total_supply) "
            "SELECT datetime('now'), COALESCE(AVG(price), 0), "
            "(SELECT COALESCE(SUM(amount), 0) FROM users) "
            "FROM stocks WHERE is_active = 1"
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("market_snapshots")
