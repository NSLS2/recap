"""Add enum attribute type with options"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "9c1df8a1bc54"
down_revision = "2f4b0e2a5d3a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attribute_enum_option",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("attribute_template_id", sa.Uuid(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column(
            "create_date",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "modified_date",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["attribute_template_id"],
            ["attribute_template.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "attribute_template_id",
            "value",
            name="uq_attr_enum_option_value_per_template",
        ),
    )
    op.create_index(
        op.f("ix_attribute_enum_option_attribute_template_id"),
        "attribute_enum_option",
        ["attribute_template_id"],
        unique=False,
    )

    with op.batch_alter_table(
        "attribute_value", schema=None, recreate="always"
    ) as batch_op:
        batch_op.add_column(sa.Column("enum_option_id", sa.Uuid(), nullable=True))
        batch_op.create_index(
            op.f("ix_attribute_value_enum_option_id"),
            ["enum_option_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_attribute_value_enum_option_id",
            "attribute_enum_option",
            ["enum_option_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table(
        "attribute_value", schema=None, recreate="always"
    ) as batch_op:
        batch_op.drop_constraint(
            "fk_attribute_value_enum_option_id",
            type_="foreignkey",
        )
        batch_op.drop_index(op.f("ix_attribute_value_enum_option_id"))
        batch_op.drop_column("enum_option_id")
    op.drop_index(
        op.f("ix_attribute_enum_option_attribute_template_id"),
        table_name="attribute_enum_option",
    )
    op.drop_table("attribute_enum_option")
