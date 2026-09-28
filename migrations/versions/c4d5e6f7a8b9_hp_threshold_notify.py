"""HP猶予・通知フラグ: markets.drop_threshold_pct + stocks.zero_notified_at

Revision ID: c4d5e6f7a8b9
Revises: a9c1e5f2b4d6
Create Date: 2026-09-29

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: str | Sequence[str] | None = "a9c1e5f2b4d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "ALTER TABLE markets ADD COLUMN "
        "drop_threshold_pct REAL NOT NULL DEFAULT 5.0"
    )
    op.execute("ALTER TABLE stocks ADD COLUMN zero_notified_at TEXT NULL")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE stocks DROP COLUMN zero_notified_at")
    op.execute("ALTER TABLE markets DROP COLUMN drop_threshold_pct")
