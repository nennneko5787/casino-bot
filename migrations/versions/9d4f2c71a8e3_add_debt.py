"""Add debt column to users

Revision ID: 9d4f2c71a8e3
Revises: 7c2e9a41f0b5
Create Date: 2026-09-28

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9d4f2c71a8e3"
down_revision: str | Sequence[str] | None = "7c2e9a41f0b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE users ADD COLUMN debt INTEGER NOT NULL DEFAULT 0")


def downgrade() -> None:
    """Downgrade schema (SQLite のため debts を除いた再作成はしない).

    SQLite は DROP COLUMN に対応しているためそれを使う。
    """
    op.execute("ALTER TABLE users DROP COLUMN debt")
