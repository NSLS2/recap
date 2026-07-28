"""remove campaigns

Revision ID: 8f3c2a1b7d90
Revises: 71c5ce51c034
Create Date: 2026-07-28
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "8f3c2a1b7d90"
down_revision = "71c5ce51c034"
branch_labels = None
depends_on = None

OWNED_TABLES = (
    "process_run",
    "process_template",
    "resource_template",
    "resource",
)


def upgrade() -> None:
    connection = op.get_bind()

    for table_name in OWNED_TABLES:
        missing = connection.scalar(
            sa.text(f"SELECT count(*) FROM {table_name} WHERE namespace_id IS NULL")
        )
        if missing:
            raise RuntimeError(f"Missing namespace IDs in {table_name}: {missing}")
        orphaned = connection.scalar(
            sa.text(
                f"""
                SELECT count(*) FROM {table_name} AS owned
                WHERE NOT EXISTS (
                    SELECT 1 FROM namespace WHERE namespace.id = owned.namespace_id
                )
                """
            )
        )
        if orphaned:
            raise RuntimeError(f"Invalid namespace IDs in {table_name}: {orphaned}")

    mismatched = connection.scalar(
        sa.text(
            """
            SELECT count(*) FROM process_run
            WHERE campaign_id IS NULL OR campaign_id != namespace_id
            """
        )
    )
    if mismatched:
        raise RuntimeError(
            f"Process runs have mismatched Campaign/Namespace ownership: {mismatched}"
        )

    with op.batch_alter_table(
        "process_run", reflect_kwargs={"resolve_fks": False}
    ) as batch_op:
        batch_op.drop_column("campaign_id")
    op.drop_table("campaign")


def downgrade() -> None:
    op.create_table(
        "campaign",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("proposal", sa.String(), nullable=False),
        sa.Column("saf", sa.String(), nullable=True),
        sa.Column("meta_data", sa.JSON(), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", "proposal", name="uq_campaign_name_proposal"),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            """
            SELECT DISTINCT namespace.id, namespace.path, namespace.metadata_json
            FROM namespace
            JOIN process_run ON process_run.namespace_id = namespace.id
            """
        )
    ).mappings()
    campaign = sa.table(
        "campaign",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("proposal", sa.String()),
        sa.column("saf", sa.String()),
        sa.column("meta_data", sa.JSON()),
    )
    for row in rows:
        metadata = row["metadata_json"] or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        connection.execute(
            campaign.insert(),
            {
                "id": row["id"],
                "name": metadata.get("recap.campaign.name", row["path"]),
                "proposal": metadata.get("nsls2.proposal", row["path"]),
                "saf": metadata.get("nsls2.saf"),
                "meta_data": metadata.get("recap.campaign.metadata"),
            },
        )
    with op.batch_alter_table(
        "process_run", reflect_kwargs={"resolve_fks": False}
    ) as batch_op:
        batch_op.add_column(sa.Column("campaign_id", sa.Uuid(), nullable=True))
    connection.execute(sa.text("UPDATE process_run SET campaign_id = namespace_id"))
    with op.batch_alter_table(
        "process_run", reflect_kwargs={"resolve_fks": False}
    ) as batch_op:
        batch_op.alter_column("campaign_id", existing_type=sa.Uuid(), nullable=False)
        batch_op.create_foreign_key(
            "fk_process_run_campaign_id_campaign", "campaign", ["campaign_id"], ["id"]
        )
