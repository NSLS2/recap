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

    naming_convention = {"uq": "uq_%(table_name)s_%(column_0_name)s"}
    with op.batch_alter_table(
        "process_template", naming_convention=naming_convention
    ) as batch_op:
        batch_op.drop_constraint("uq_process_template_name", type_="unique")
        batch_op.drop_constraint("uq_process_template_name_version", type_="unique")
        batch_op.drop_column("is_active")
        batch_op.create_unique_constraint(
            "uq_process_template_namespace_name_version",
            ["namespace_id", "name", "version"],
        )

    with op.batch_alter_table(
        "process_run", naming_convention=naming_convention
    ) as batch_op:
        batch_op.drop_constraint("uq_process_run_name", type_="unique")
        batch_op.create_unique_constraint(
            "uq_process_run_namespace_name", ["namespace_id", "name"]
        )

    with op.batch_alter_table("resource_template") as batch_op:
        batch_op.drop_constraint(
            "uq_resource_template_parent_name_version", type_="unique"
        )
        batch_op.create_unique_constraint(
            "uq_resource_template_namespace_parent_name_version",
            ["namespace_id", "parent_id", "name", "version"],
        )

    with op.batch_alter_table(
        "resource", reflect_kwargs={"resolve_fks": False}
    ) as batch_op:
        batch_op.drop_column("active")

    with op.batch_alter_table("resource") as batch_op:
        batch_op.add_column(sa.Column("copied_from_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_resource_copied_from_id_resource",
            "resource",
            ["copied_from_id"],
            ["id"],
        )

    for table_name in ("process_template", "resource_template"):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.add_column(
                sa.Column("labels", sa.JSON(), nullable=False, server_default="[]")
            )

    connection = op.get_bind()
    options = context.get_context().opts
    base_path = options.get("base_namespace_path", "")
    if base_path is None:
        base_path = ""
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
        default_campaign_path = (
            f"campaign/{campaign_id}"
            if base_path == ""
            else f"{base_path}/campaign/{campaign_id}"
        )
        campaign_path = configured_paths.get(str(campaign_id), default_campaign_path)
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
                "path": campaign_path,
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
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column(
                "namespace_id", existing_type=sa.Uuid(), nullable=False
            )
            batch_op.alter_column("status", existing_type=sa.String(), nullable=False)
            batch_op.alter_column(
                "revision", existing_type=sa.Integer(), nullable=False
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
        batch_op.add_column(
            sa.Column("active", sa.Boolean(), nullable=False, server_default="1")
        )

    with op.batch_alter_table(
        "resource_template", reflect_kwargs={"resolve_fks": False}
    ) as batch_op:
        batch_op.drop_constraint(
            "uq_resource_template_namespace_parent_name_version", type_="unique"
        )
        batch_op.create_unique_constraint(
            "uq_resource_template_parent_name_version",
            ["parent_id", "name", "version"],
        )

    with op.batch_alter_table(
        "process_run", reflect_kwargs={"resolve_fks": False}
    ) as batch_op:
        batch_op.drop_constraint("uq_process_run_namespace_name", type_="unique")
        batch_op.create_unique_constraint("uq_process_run_name", ["name"])

    with op.batch_alter_table(
        "process_template", reflect_kwargs={"resolve_fks": False}
    ) as batch_op:
        batch_op.drop_constraint(
            "uq_process_template_namespace_name_version", type_="unique"
        )
        batch_op.add_column(
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="0")
        )
        batch_op.create_unique_constraint("uq_process_template_name", ["name"])
        batch_op.create_unique_constraint(
            "uq_process_template_name_version", ["name", "version"]
        )

    for table_name in ("resource_template", "process_template"):
        with op.batch_alter_table(
            table_name, reflect_kwargs={"resolve_fks": False}
        ) as batch_op:
            batch_op.drop_column("labels")

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
