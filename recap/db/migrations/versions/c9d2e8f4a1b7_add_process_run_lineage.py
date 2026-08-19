"""add process run copy lineage

Revision ID: c9d2e8f4a1b7
Revises: b47f5e2a9c10
"""

import sqlalchemy as sa
from alembic import op

revision = "c9d2e8f4a1b7"
down_revision = "b47f5e2a9c10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("process_run") as batch:
        batch.add_column(sa.Column("copied_from_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_process_run_copied_from", "process_run", ["copied_from_id"], ["id"]
        )
        batch.drop_constraint("uq_process_run_namespace_name", type_="unique")
    op.create_index(
        "uq_process_run_namespace_name_root",
        "process_run",
        ["namespace_id", "name"],
        unique=True,
        sqlite_where=sa.text("copied_from_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_process_run_namespace_name_root", table_name="process_run")
    op.create_unique_constraint(
        "uq_process_run_namespace_name", "process_run", ["namespace_id", "name"]
    )
    op.drop_constraint("fk_process_run_copied_from", "process_run", type_="foreignkey")
    op.drop_column("process_run", "copied_from_id")
