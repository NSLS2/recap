"""Scope step names to runs for roots and to parents for runtime children."""

import sqlalchemy as sa
from alembic import op

revision = "a12f9d4b7e30"
down_revision = "c9d2e8f4a1b7"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    invalid = connection.execute(
        sa.text("""
        SELECT child.id FROM step AS child JOIN step AS parent ON child.parent_id = parent.id
        WHERE parent.parent_id IS NOT NULL OR child.process_run_id != parent.process_run_id
           OR child.step_template_id != parent.step_template_id
    """)
    ).first()
    duplicates = connection.execute(
        sa.text("""
        SELECT process_run_id, parent_id, name FROM step
        GROUP BY process_run_id, parent_id, name HAVING COUNT(*) > 1
    """)
    ).first()
    if invalid or duplicates:
        raise ValueError(
            "Existing step hierarchy violates parent/template or scoped-name constraints"
        )
    with op.batch_alter_table("step") as batch:
        batch.create_unique_constraint("uq_step_parent_name", ["parent_id", "name"])
    op.create_index(
        "uq_step_run_root_name",
        "step",
        ["process_run_id", "name"],
        unique=True,
        sqlite_where=sa.text("parent_id IS NULL"),
        postgresql_where=sa.text("parent_id IS NULL"),
    )


def downgrade():
    op.drop_index("uq_step_run_root_name", table_name="step")
    with op.batch_alter_table("step") as batch:
        batch.drop_constraint("uq_step_parent_name", type_="unique")
