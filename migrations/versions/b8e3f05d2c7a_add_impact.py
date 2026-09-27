"""Add impact column to stocks (per-share price sensitivity)

Revision ID: b8e3f05d2c7a
Revises: 9d4f2c71a8e3
Create Date: 2026-09-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8e3f05d2c7a"
down_revision: str | Sequence[str] | None = "9d4f2c71a8e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ticker ごとの初期 impact (1株あたりの変動率)。流動性の個性付け。
SEED_IMPACT = {
    "SUMANPO": 0.0003,
    "NEKO": 0.0005,
    "MOON": 0.001,
    "OKURA": 0.0002,
    "TAKA": 0.0005,
    "PRECURE": 0.002,
    "RAKUTEN": 0.0003,
    "GMO": 0.0008,
}


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE stocks ADD COLUMN impact REAL NOT NULL DEFAULT 0.0005")
    for ticker, impact in SEED_IMPACT.items():
        op.execute(
            sa.text(
                "UPDATE stocks SET impact = :impact WHERE ticker = :ticker"
            ).bindparams(impact=impact, ticker=ticker)
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE stocks DROP COLUMN impact")
