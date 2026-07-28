"""add namespaces

Revision ID: 71c5ce51c034
Revises: f11ecd5c55cf
Create Date: 2026-07-27 22:38:56.480008

"""

import json
from uuid import NAMESPACE_URL, UUID, uuid5

import sqlalchemy as sa
from alembic import context, op

# revision identifiers, used by Alembic.
revision = "71c5ce51c034"
down_revision = "f11ecd5c55cf"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "namespace",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "create_date",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "modified_date",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["parent_id"], ["namespace.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("path"),
    )

    for table_name in (
        "process_run",
        "process_template",
        "resource_template",
        "resource",
    ):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.add_column(sa.Column("namespace_id", sa.Uuid(), nullable=True))
            batch_op.add_column(sa.Column("status", sa.String(), nullable=True))
            batch_op.add_column(sa.Column("revision", sa.Integer(), nullable=True))
            batch_op.create_foreign_key(
                f"fk_{table_name}_namespace_id_namespace",
                "namespace",
                ["namespace_id"],
                ["id"],
            )

    with op.batch_alter_table("resource") as batch_op:
        batch_op.add_column(sa.Column("copied_from_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_resource_copied_from_id_resource",
            "resource",
            ["copied_from_id"],
            ["id"],
        )

    connection = op.get_bind()
    options = context.get_context().opts
    base_path = options.get("base_namespace_path") or "default"
    configured_paths = options.get("campaign_namespace_paths") or {}
    base_id = uuid5(NAMESPACE_URL, f"recap:namespace:{base_path}")
    namespace = sa.table(
        "namespace",
        sa.column("id", sa.Uuid()),
        sa.column("path", sa.String()),
        sa.column("parent_id", sa.Uuid()),
        sa.column("metadata_json", sa.JSON()),
        sa.column("status", sa.String()),
        sa.column("revision", sa.Integer()),
    )
    connection.execute(
        namespace.insert(),
        {
            "id": base_id,
            "path": base_path,
            "parent_id": None,
            "metadata_json": {},
            "status": "MUTABLE",
            "revision": 1,
        },
    )

    campaigns = connection.execute(
        sa.text("SELECT id, name, proposal, saf, meta_data FROM campaign")
    ).mappings()
    for campaign in campaigns:
        campaign_id = UUID(str(campaign["id"]))
        campaign_metadata = campaign["meta_data"]
        if isinstance(campaign_metadata, str):
            campaign_metadata = json.loads(campaign_metadata)
        metadata = {
            "recap.campaign.name": campaign["name"],
            "nsls2.proposal": campaign["proposal"],
            "nsls2.saf": campaign["saf"],
            "recap.campaign.metadata": campaign_metadata,
        }
        connection.execute(
            namespace.insert(),
            {
                "id": campaign_id,
                "path": configured_paths.get(
                    str(campaign_id), f"{base_path}/campaign/{campaign_id}"
                ),
                "parent_id": base_id,
                "metadata_json": metadata,
                "status": "ACTIVE",
                "revision": 1,
            },
        )

    connection.execute(
        sa.text(
            """
            UPDATE process_run
            SET namespace_id = campaign_id, status = 'ACTIVE', revision = 1
            """
        )
    )
    for table_name in ("process_template", "resource_template"):
        connection.execute(
            sa.text(
                f"""
                UPDATE {table_name}
                SET namespace_id = :base_id, status = 'MUTABLE', revision = 1
                """
            ).bindparams(sa.bindparam("base_id", type_=sa.Uuid())),
            {"base_id": base_id},
        )
    connection.execute(
        sa.text(
            """
            UPDATE process_template
            SET status = 'ACTIVE'
            WHERE id IN (SELECT process_template_id FROM process_run)
            """
        )
    )
    connection.execute(
        sa.text(
            """
            UPDATE resource_template
            SET status = 'ACTIVE'
            WHERE id IN (
                SELECT resource_template_id FROM resource
                WHERE resource_template_id IS NOT NULL
            )
            """
        )
    )
    connection.execute(
        sa.text(
            """
            UPDATE resource
            SET namespace_id = :base_id,
                status = CASE
                    WHEN id IN (SELECT resource_id FROM resource_assignment)
                    THEN 'ACTIVE'
                    ELSE 'MUTABLE'
                END,
                revision = 1
            """
        ).bindparams(sa.bindparam("base_id", type_=sa.Uuid())),
        {"base_id": base_id},
    )

    for table_name in (
        "process_run",
        "process_template",
        "resource_template",
        "resource",
    ):
        missing = connection.scalar(
            sa.text(f"SELECT count(*) FROM {table_name} WHERE namespace_id IS NULL")
        )
        if missing:
            raise RuntimeError(f"Missing namespace IDs in {table_name}: {missing}")


def downgrade() -> None:
    with op.batch_alter_table(
        "resource", reflect_kwargs={"resolve_fks": False}
    ) as batch_op:
        batch_op.drop_constraint(
            "fk_resource_copied_from_id_resource", type_="foreignkey"
        )
        batch_op.drop_column("copied_from_id")

    for table_name in reversed(
        (
            "process_run",
            "process_template",
            "resource_template",
            "resource",
        )
    ):
        with op.batch_alter_table(
            table_name, reflect_kwargs={"resolve_fks": False}
        ) as batch_op:
            batch_op.drop_constraint(
                f"fk_{table_name}_namespace_id_namespace", type_="foreignkey"
            )
            batch_op.drop_column("revision")
            batch_op.drop_column("status")
            batch_op.drop_column("namespace_id")

    if sa.inspect(op.get_bind()).has_table("namespace"):
        op.drop_table("namespace")
