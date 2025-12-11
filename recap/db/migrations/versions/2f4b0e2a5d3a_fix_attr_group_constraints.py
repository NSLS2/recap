"""Enforce exclusive owner and clean duplicate attribute groups

Revision ID: 2f4b0e2a5d3a
Revises: 8c2bf3e2d9f2
Create Date: 2026-03-11 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "2f4b0e2a5d3a"
down_revision = "8c2bf3e2d9f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    agt = sa.table(
        "attribute_group_template",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("resource_template_id", sa.Uuid()),
        sa.column("step_template_id", sa.Uuid()),
        sa.column("create_date", sa.DateTime(timezone=True)),
    )

    # Remove duplicate attribute groups per owner/name to allow constraints to be applied.
    res_subq = (
        sa.select(
            agt.c.id,
            sa.func.row_number()
            .over(
                partition_by=[agt.c.resource_template_id, agt.c.name],
                order_by=agt.c.create_date,
            )
            .label("rn"),
        )
        .where(agt.c.resource_template_id.isnot(None))
        .subquery()
    )
    op.execute(
        agt.delete().where(
            agt.c.id.in_(sa.select(res_subq.c.id).where(res_subq.c.rn > 1))
        )
    )

    step_subq = (
        sa.select(
            agt.c.id,
            sa.func.row_number()
            .over(
                partition_by=[agt.c.step_template_id, agt.c.name],
                order_by=agt.c.create_date,
            )
            .label("rn"),
        )
        .where(agt.c.step_template_id.isnot(None))
        .subquery()
    )
    op.execute(
        agt.delete().where(
            agt.c.id.in_(sa.select(step_subq.c.id).where(step_subq.c.rn > 1))
        )
    )

    with op.batch_alter_table("attribute_group_template") as batch_op:
        # Drop the older "at most one owner" constraint and replace with XOR.
        batch_op.drop_constraint("ck_attr_group_at_most_one_owner", type_="check")
        batch_op.create_check_constraint(
            "ck_attr_group_exactly_one_owner",
            "(resource_template_id IS NOT NULL) <> (step_template_id IS NOT NULL)",
        )


def downgrade() -> None:
    with op.batch_alter_table("attribute_group_template") as batch_op:
        batch_op.drop_constraint("ck_attr_group_exactly_one_owner", type_="check")
        batch_op.create_check_constraint(
            "ck_attr_group_at_most_one_owner",
            "(resource_template_id IS NULL) OR (step_template_id IS NULL)",
        )
