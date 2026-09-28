"""Add start_price to stocks (bankruptcy danger-zone baseline)

Revision ID: c3d4e5f6a7b8
Revises: a1b2c3d4e5f6
Create Date: 2026-09-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # 開始価格 (倒産の危険水域判定の基準)。既存行は履歴先頭≒設立時価格で埋める。
    op.execute("ALTER TABLE stocks ADD COLUMN start_price INTEGER NOT NULL DEFAULT 100")
    op.execute(
        sa.text(
            "UPDATE stocks SET start_price = COALESCE("
            "(SELECT price FROM stock_history WHERE ticker = stocks.ticker "
            "ORDER BY id ASC LIMIT 1), price, 100)"
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE stocks DROP COLUMN start_price")
