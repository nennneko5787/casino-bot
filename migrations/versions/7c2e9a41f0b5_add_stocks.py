"""Add stocks tables + seed 8 tickers

Revision ID: 7c2e9a41f0b5
Revises: 3fa5003b63e3
Create Date: 2026-09-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c2e9a41f0b5"
down_revision: str | Sequence[str] | None = "3fa5003b63e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 初期8銘柄: 銘柄ごとに異なる値動きになるよう mu/sigma を分散させる
SEED_STOCKS = [
    # ticker, display_name, price, mu, sigma
    ("SUMANPO", "SUMANPO", 1000, 0.0005, 0.03),
    ("NEKO", "NEKO", 800, 0.001, 0.05),
    ("MOON", "MOON", 500, 0.002, 0.09),
    ("OKURA", "OKURA", 2000, -0.0005, 0.02),
    ("TAKA", "TAKA", 1200, 0.0, 0.06),
    ("PRECURE", "PRECURE", 300, 0.003, 0.12),
    ("RAKUTEN", "RAKUTEN", 1500, 0.0002, 0.04),
    ("GMO", "GMO", 950, -0.001, 0.07),
]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "stocks",
        sa.Column("ticker", sa.TEXT(), primary_key=True),
        sa.Column("display_name", sa.TEXT(), nullable=False),
        sa.Column("price", sa.INTEGER(), nullable=False),
        sa.Column("mu", sa.REAL(), nullable=False, server_default="0"),
        sa.Column("sigma", sa.REAL(), nullable=False, server_default="0.05"),
        sa.Column("is_active", sa.INTEGER(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.TEXT(), nullable=False),
    )
    op.create_table(
        "stock_history",
        sa.Column("id", sa.INTEGER(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.TEXT(), nullable=False),
        sa.Column("price", sa.INTEGER(), nullable=False),
        sa.Column("created_at", sa.TEXT(), nullable=False),
    )
    op.create_index("ix_stock_history_ticker", "stock_history", ["ticker"])
    op.create_table(
        "holdings",
        sa.Column("user_id", sa.BIGINT(), nullable=False),
        sa.Column("ticker", sa.TEXT(), nullable=False),
        sa.Column("qty", sa.INTEGER(), nullable=False),
        sa.Column("avg_cost", sa.INTEGER(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "ticker"),
    )

    now = "2026-09-28T00:00:00"
    for ticker, name, price, mu, sigma in SEED_STOCKS:
        op.execute(
            sa.text(
                "INSERT OR IGNORE INTO stocks "
                "(ticker, display_name, price, mu, sigma, is_active, updated_at) "
                "VALUES (:ticker, :name, :price, :mu, :sigma, 1, :now)"
            ).bindparams(
                ticker=ticker, name=name, price=price, mu=mu, sigma=sigma, now=now
            )
        )
        op.execute(
            sa.text(
                "INSERT INTO stock_history (ticker, price, created_at) "
                "VALUES (:ticker, :price, :now)"
            ).bindparams(ticker=ticker, price=price, now=now)
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("holdings")
    op.drop_index("ix_stock_history_ticker", table_name="stock_history")
    op.drop_table("stock_history")
    op.drop_table("stocks")
