"""add_sentinel

Revision ID: 42c9cafa5e9f
Revises: 6f71f1cb4edb
Create Date: 2025-12-06 12:55:58.995462

"""

from uuid import UUID

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "42c9cafa5e9f"
down_revision = "6f71f1cb4edb"
branch_labels = None
depends_on = None

ROOT_ID = UUID("00000000-0000-0000-0000-000000000001")


def upgrade() -> None:
    conn = op.get_bind()
    resource_template = sa.table(
        "resource_template",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("slug", sa.String()),
        sa.column("parent_id", sa.Uuid()),
    )
    insert_stmt = sa.insert(resource_template).values(
        id=ROOT_ID,
        name="__root__",
        slug="__root__",
        parent_id=None,
    )
    conn.execute(insert_stmt)


def downgrade() -> None:
    conn = op.get_bind()
    resource_template = sa.table("resource_template", sa.column("id", sa.Uuid()))
    delete_stmt = sa.delete(resource_template).where(resource_template.c.id == ROOT_ID)
    conn.execute(delete_stmt)
