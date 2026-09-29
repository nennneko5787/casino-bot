"""Add company accounts (treasury/members/invites/withdraw/tax ledger)

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-09-29

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: str | Sequence[str] | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "company_accounts",
        sa.Column("ticker", sa.TEXT(), primary_key=True),
        sa.Column("balance", sa.INTEGER(), nullable=False, server_default="0"),
        sa.Column("tax_arrears", sa.INTEGER(), nullable=False, server_default="0"),
        sa.Column("tax_missed", sa.INTEGER(), nullable=False, server_default="0"),
        sa.Column("last_taxed_at", sa.TEXT(), nullable=True),
        sa.Column("updated_at", sa.TEXT(), nullable=False),
    )
    op.create_table(
        "company_members",
        sa.Column("ticker", sa.TEXT(), nullable=False),
        sa.Column("user_id", sa.BIGINT(), nullable=False),
        sa.Column("role", sa.TEXT(), nullable=False, server_default="member"),
        sa.Column("created_at", sa.TEXT(), nullable=False),
        sa.PrimaryKeyConstraint("ticker", "user_id"),
    )
    op.create_table(
        "company_invites",
        sa.Column("ticker", sa.TEXT(), nullable=False),
        sa.Column("user_id", sa.BIGINT(), nullable=False),
        sa.Column("invited_by", sa.BIGINT(), nullable=False),
        sa.Column("created_at", sa.TEXT(), nullable=False),
        sa.PrimaryKeyConstraint("ticker", "user_id"),
    )
    op.create_table(
        "withdraw_requests",
        sa.Column("id", sa.INTEGER(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.TEXT(), nullable=False),
        sa.Column("requester", sa.BIGINT(), nullable=False),
        sa.Column("amount", sa.INTEGER(), nullable=False),
        sa.Column("status", sa.TEXT(), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.TEXT(), nullable=False),
        sa.Column("decided_at", sa.TEXT(), nullable=True),
        sa.Column("decided_by", sa.BIGINT(), nullable=True),
    )
    op.create_index("ix_withdraw_requests_ticker", "withdraw_requests", ["ticker"])
    op.create_table(
        "company_ledger",
        sa.Column("id", sa.INTEGER(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.TEXT(), nullable=False),
        sa.Column("user_id", sa.BIGINT(), nullable=False),
        sa.Column("kind", sa.TEXT(), nullable=False),
        sa.Column("amount", sa.INTEGER(), nullable=False),
        sa.Column("created_at", sa.TEXT(), nullable=False),
    )
    op.create_index("ix_company_ledger_ticker", "company_ledger", ["ticker"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_company_ledger_ticker", table_name="company_ledger")
    op.drop_table("company_ledger")
    op.drop_index("ix_withdraw_requests_ticker", table_name="withdraw_requests")
    op.drop_table("withdraw_requests")
    op.drop_table("company_invites")
    op.drop_table("company_members")
    op.drop_table("company_accounts")
