import json
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text

from recap.utils.migrations import apply_migrations


def test_campaign_data_is_backfilled_into_namespaces(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'migration.db'}"
    campaign_id = uuid4()
    process_template_id = uuid4()
    process_run_id = uuid4()
    resource_type_id = uuid4()
    resource_template_id = uuid4()
    resource_slot_id = uuid4()
    assigned_resource_id = uuid4()
    unassigned_resource_id = uuid4()

    apply_migrations(db_url, revision="f11ecd5c55cf")
    engine = create_engine(db_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO campaign (id, name, proposal, saf, meta_data)
                VALUES (:id, 'Run 2026-1', '312345', '310000', :metadata)
                """
            ),
            {"id": campaign_id.hex, "metadata": json.dumps({"sample": "Ni"})},
        )
        connection.execute(
            text(
                """
                INSERT INTO process_template (id, name, version, is_active)
                VALUES (:id, 'scan', '1.0', 1)
                """
            ),
            {"id": process_template_id.hex},
        )
        connection.execute(
            text(
                """
                INSERT INTO process_run
                    (id, name, description, process_template_id, campaign_id)
                VALUES (:id, 'scan-1', '', :template_id, :campaign_id)
                """
            ),
            {
                "id": process_run_id.hex,
                "template_id": process_template_id.hex,
                "campaign_id": campaign_id.hex,
            },
        )
        connection.execute(
            text("INSERT INTO resource_type (id, name) VALUES (:id, 'sample')"),
            {"id": resource_type_id.hex},
        )
        connection.execute(
            text(
                """
                INSERT INTO resource_template (id, name, version)
                VALUES (:id, 'sample', '1.0')
                """
            ),
            {"id": resource_template_id.hex},
        )
        connection.execute(
            text(
                """
                INSERT INTO resource_slot
                    (id, name, process_template_id, resource_type_id, direction)
                VALUES (:id, 'sample', :template_id, :type_id, 'input')
                """
            ),
            {
                "id": resource_slot_id.hex,
                "template_id": process_template_id.hex,
                "type_id": resource_type_id.hex,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO resource
                    (id, name, active, resource_template_id)
                VALUES
                    (:assigned_id, 'assigned', 1, :template_id),
                    (:unassigned_id, 'unassigned', 1, :template_id)
                """
            ),
            {
                "assigned_id": assigned_resource_id.hex,
                "unassigned_id": unassigned_resource_id.hex,
                "template_id": resource_template_id.hex,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO resource_assignment
                    (id, process_run_id, resource_slot_id, resource_id)
                VALUES (:id, :run_id, :slot_id, :resource_id)
                """
            ),
            {
                "id": uuid4().hex,
                "run_id": process_run_id.hex,
                "slot_id": resource_slot_id.hex,
                "resource_id": assigned_resource_id.hex,
            },
        )

    campaign_path = f"default/campaign/{campaign_id}"
    apply_migrations(
        db_url,
        campaign_namespace_paths={campaign_id: campaign_path},
        base_namespace_path="default",
    )

    with engine.connect() as connection:
        namespace = connection.execute(
            text("SELECT id, path, metadata_json FROM namespace WHERE id = :id"),
            {"id": campaign_id.hex},
        ).mappings().one()
        process_run = connection.execute(
            text("SELECT namespace_id FROM process_run WHERE id = :id"),
            {"id": process_run_id.hex},
        ).mappings().one()
        resources = {
            row["id"]: row
            for row in connection.execute(
                text("SELECT id, namespace_id, status, revision FROM resource")
            ).mappings()
        }
        templates = connection.execute(
            text(
                """
                SELECT
                    (SELECT namespace_id FROM process_template WHERE id = :process_id)
                        AS process_namespace_id,
                    (SELECT namespace_id FROM resource_template WHERE id = :resource_id)
                        AS resource_namespace_id
                """
            ),
            {
                "process_id": process_template_id.hex,
                "resource_id": resource_template_id.hex,
            },
        ).mappings().one()

    metadata = json.loads(namespace["metadata_json"])
    assert namespace["path"] == campaign_path
    assert namespace["id"] == campaign_id.hex
    assert process_run["namespace_id"] == namespace["id"]
    assert resources[assigned_resource_id.hex]["status"] == "ACTIVE"
    assert resources[unassigned_resource_id.hex]["status"] == "MUTABLE"
    assert metadata["recap.campaign.name"] == "Run 2026-1"
    assert metadata["nsls2.proposal"] == "312345"
    assert metadata["nsls2.saf"] == "310000"
    assert metadata["recap.campaign.metadata"] == {"sample": "Ni"}
    assert templates["process_namespace_id"] == templates["resource_namespace_id"]
    assert resources[assigned_resource_id.hex]["revision"] == 1

    campaign_columns = {column["name"] for column in inspect(engine).get_columns("campaign")}
    assert {"name", "proposal", "saf", "meta_data"} <= campaign_columns
