"""HP猶予・通知フラグ: markets.drop_threshold_pct + stocks.zero_notified_at

Revision ID: c4d5e6f7a8b9
Revises: a9c1e5f2b4d6
Create Date: 2026-09-29

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: str | Sequence[str] | None = "a9c1e5f2b4d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table: str) -> set[str]:
    """既存テーブルにすでにある列名集合 (二重追加防止用)。"""
    return {col["name"] for col in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    """Upgrade schema."""
    # services/stocks.py の ensure_market_schema() が先に列を
    # 作っているDBがあるため、既存列は追加しない。
    if "drop_threshold_pct" not in _columns("markets"):
        op.execute(
            "ALTER TABLE markets ADD COLUMN "
            "drop_threshold_pct REAL NOT NULL DEFAULT 5.0"
        )
    if "zero_notified_at" not in _columns("stocks"):
        op.execute("ALTER TABLE stocks ADD COLUMN zero_notified_at TEXT NULL")


def downgrade() -> None:
    """Downgrade schema."""
    if "zero_notified_at" in _columns("stocks"):
        op.execute("ALTER TABLE stocks DROP COLUMN zero_notified_at")
    if "drop_threshold_pct" in _columns("markets"):
        op.execute("ALTER TABLE markets DROP COLUMN drop_threshold_pct")
