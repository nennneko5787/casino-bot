"""Add owner_id + floor_since to stocks (user companies / bankruptcy)

Revision ID: d2e4f6a8b0c1
Revises: f1a2b3c4d5e6
Create Date: 2026-09-28

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2e4f6a8b0c1"
down_revision: str | Sequence[str] | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # 設立者のユーザーID (NULL=運営銘柄)
    op.execute("ALTER TABLE stocks ADD COLUMN owner_id BIGINT NULL")
    # 価格1に落ちた時刻 (破産判定用。1超でNULLに戻る)
    op.execute("ALTER TABLE stocks ADD COLUMN floor_since TEXT NULL")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE stocks DROP COLUMN floor_since")
    op.execute("ALTER TABLE stocks DROP COLUMN owner_id")
