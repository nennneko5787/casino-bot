"""HP緩和: 猶予48h・減少1.5に更新 (HP制は維持)

Revision ID: a9c1e5f2b4d6
Revises: f2b7c8d9e0a1
Create Date: 2026-09-29

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a9c1e5f2b4d6"
down_revision: str | Sequence[str] | None = "f2b7c8d9e0a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # 旧既定値のままの市場だけ新値へ。管理者がカスタム済みの市場は触らない。
    op.execute(
        "UPDATE markets SET dmg_per_pct = 1.5, "
        "rescue_hours = 48.0, zero_grace_hours = 48.0 "
        "WHERE dmg_per_pct = 3.0 AND rescue_hours = 24.0 "
        "AND zero_grace_hours = 1.0"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "UPDATE markets SET dmg_per_pct = 3.0, "
        "rescue_hours = 24.0, zero_grace_hours = 1.0 "
        "WHERE dmg_per_pct = 1.5 AND rescue_hours = 48.0 "
        "AND zero_grace_hours = 48.0"
    )
