"""Add resource template versioning

Revision ID: 8c2bf3e2d9f2
Revises: da61d550b9dc
Create Date: 2026-02-23 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "8c2bf3e2d9f2"
down_revision = "da61d550b9dc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Add column nullable with a default so existing rows pick up a value
    op.add_column(
        "resource_template",
        sa.Column("version", sa.String(), nullable=True, server_default="1.0"),
    )
    # 2) Backfill any NULLs just in case
    resource_template = sa.table(
        "resource_template",
        sa.column("version", sa.String()),
    )
    op.execute(
        resource_template.update()
        .where(resource_template.c.version.is_(None))
        .values(version="1.0")
    )
    # 3) Remove the default and enforce NOT NULL via batch (SQLite-friendly)
    with op.batch_alter_table("resource_template") as batch_op:
        batch_op.alter_column(
            "version",
            existing_type=sa.String(),
            nullable=False,
            server_default=None,
        )
        batch_op.drop_constraint(
            "uq_resource_template_parent_name",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_resource_template_parent_name_version",
            ["parent_id", "name", "version"],
        )


def downgrade() -> None:
    with op.batch_alter_table("resource_template") as batch_op:
        batch_op.create_unique_constraint(
            "uq_resource_template_parent_name",
            ["parent_id", "name"],
        )
        batch_op.drop_constraint(
            "uq_resource_template_parent_name_version",
            type_="unique",
        )
        batch_op.drop_column("version")
