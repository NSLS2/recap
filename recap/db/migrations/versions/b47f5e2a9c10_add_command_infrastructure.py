"""add command infrastructure

Revision ID: b47f5e2a9c10
Revises: 8f3c2a1b7d90
Create Date: 2026-07-28
"""

import sqlalchemy as sa
from alembic import op

revision = "b47f5e2a9c10"
down_revision = "8f3c2a1b7d90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "command_idempotency",
        sa.Column("actor_id", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(), nullable=True),
        sa.Column("response_json", sa.JSON(), nullable=True),
        sa.Column(
            "create_date",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("actor_id", "idempotency_key"),
    )
    op.create_table(
        "mutation_audit",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=True),
        sa.Column("mutation", sa.String(), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("reason_code", sa.String(), nullable=True),
        sa.Column(
            "create_date",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("mutation_audit")
    op.drop_table("command_idempotency")
