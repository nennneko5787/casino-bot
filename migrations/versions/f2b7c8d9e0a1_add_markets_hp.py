"""Add markets table + stocks market_id/hp/crisis_since (HP bankruptcy)

Revision ID: f2b7c8d9e0a1
Revises: c3d4e5f6a7b8
Create Date: 2026-09-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2b7c8d9e0a1"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 初期3市場: 値動きレンジ (mu/sigma/impact) と HP倒産パラメータを持つ。
# id, display_name, description,
# mu_min, mu_max, sigma_min, sigma_max, impact_min, impact_max, jitter,
# hp_max, warning_hp, dmg_per_pct, recover_per_pct,
# rescue_hp_per_100, rescue_hours, zero_grace_hours
SEED_MARKETS = [
    (
        "MEOWDAQ", "MEOWDAQ", "メインの穏やかな市場",
        -0.001, 0.002, 0.02, 0.05, 0.0002, 0.0006, 0.0005,
        100, 30, 1.5, 2.0, 10.0, 48.0, 48.0,
    ),
    (
        "AABOT", "AABOT", "値動きの荒い市場",
        -0.002, 0.003, 0.05, 0.10, 0.0005, 0.002, 0.001,
        100, 30, 1.5, 2.0, 10.0, 48.0, 48.0,
    ),
    (
        "OZETUDO", "OZETUDO", "値動きの鈍い安定市場",
        -0.0005, 0.001, 0.01, 0.03, 0.0001, 0.0003, 0.0002,
        100, 30, 1.5, 2.0, 10.0, 48.0, 48.0,
    ),
]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "markets",
        sa.Column("id", sa.TEXT(), primary_key=True),
        sa.Column("display_name", sa.TEXT(), nullable=False),
        sa.Column("description", sa.TEXT(), nullable=False, server_default=""),
        sa.Column("mu_min", sa.REAL(), nullable=False, server_default="0"),
        sa.Column("mu_max", sa.REAL(), nullable=False, server_default="0"),
        sa.Column("sigma_min", sa.REAL(), nullable=False, server_default="0.05"),
        sa.Column("sigma_max", sa.REAL(), nullable=False, server_default="0.05"),
        sa.Column("impact_min", sa.REAL(), nullable=False, server_default="0.0005"),
        sa.Column("impact_max", sa.REAL(), nullable=False, server_default="0.0005"),
        sa.Column("jitter", sa.REAL(), nullable=False, server_default="0"),
        sa.Column("hp_max", sa.INTEGER(), nullable=False, server_default="100"),
        sa.Column("warning_hp", sa.INTEGER(), nullable=False, server_default="30"),
        sa.Column("dmg_per_pct", sa.REAL(), nullable=False, server_default="1.5"),
        sa.Column("recover_per_pct", sa.REAL(), nullable=False, server_default="2.0"),
        sa.Column("rescue_hp_per_100", sa.REAL(), nullable=False, server_default="10.0"),
        sa.Column("rescue_hours", sa.REAL(), nullable=False, server_default="48.0"),
        sa.Column("zero_grace_hours", sa.REAL(), nullable=False, server_default="48.0"),
        sa.Column("mean_ref_price", sa.REAL(), nullable=True),
        sa.Column("mean_k", sa.REAL(), nullable=True),
        sa.Column("updated_at", sa.TEXT(), nullable=False),
    )
    now = "2026-09-28T00:00:00"
    for row in SEED_MARKETS:
        op.execute(
            sa.text(
                "INSERT OR IGNORE INTO markets "
                "(id, display_name, description, mu_min, mu_max, "
                "sigma_min, sigma_max, impact_min, impact_max, jitter, "
                "hp_max, warning_hp, dmg_per_pct, recover_per_pct, "
                "rescue_hp_per_100, rescue_hours, zero_grace_hours, "
                "updated_at) "
                "VALUES (:id, :name, :desc, :mu_min, :mu_max, "
                ":sigma_min, :sigma_max, :impact_min, :impact_max, :jitter, "
                ":hp_max, :warning_hp, :dmg, :rec, "
                ":rescue, :rescue_h, :zero_h, :now)"
            ).bindparams(
                id=row[0], name=row[1], desc=row[2],
                mu_min=row[3], mu_max=row[4],
                sigma_min=row[5], sigma_max=row[6],
                impact_min=row[7], impact_max=row[8], jitter=row[9],
                hp_max=row[10], warning_hp=row[11], dmg=row[12], rec=row[13],
                rescue=row[14], rescue_h=row[15], zero_h=row[16], now=now,
            )
        )
    # 銘柄側: 所属市場・HP・危機突入時刻 (追証期限の基準)・設立ランク
    op.execute("ALTER TABLE stocks ADD COLUMN market_id TEXT NULL")
    op.execute("ALTER TABLE stocks ADD COLUMN hp INTEGER NULL")
    op.execute("ALTER TABLE stocks ADD COLUMN crisis_since TEXT NULL")
    op.execute("ALTER TABLE stocks ADD COLUMN rank TEXT NULL")
    op.execute("UPDATE stocks SET market_id = 'MEOWDAQ' WHERE market_id IS NULL")
    op.execute("UPDATE stocks SET hp = 100 WHERE hp IS NULL")
    op.execute("UPDATE stocks SET rank = 'B' WHERE rank IS NULL")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE stocks DROP COLUMN rank")
    op.execute("ALTER TABLE stocks DROP COLUMN crisis_since")
    op.execute("ALTER TABLE stocks DROP COLUMN hp")
    op.execute("ALTER TABLE stocks DROP COLUMN market_id")
    op.drop_table("markets")
