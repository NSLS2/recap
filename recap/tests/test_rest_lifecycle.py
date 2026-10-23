import pytest

from .command_responses import command_data


def _seed_lifecycle_entity(
    api_client, idempotency_headers, create_namespace, object_type
):
    create_namespace()
    if object_type == "resource_template":
        response = api_client.post(
            "/api/v1/resource-templates/beamline/amx",
            headers=idempotency_headers("template"),
            json={"name": "plate", "version": "1", "type_names": ["plate"]},
        )
    elif object_type == "resource":
        template = api_client.post(
            "/api/v1/resource-templates/beamline/amx",
            headers=idempotency_headers("template"),
            json={"name": "plate", "version": "1", "type_names": ["plate"]},
        )
        response = api_client.post(
            "/api/v1/resources/beamline/amx",
            headers=idempotency_headers("resource"),
            json={"name": "sample", "template_id": command_data(template)["id"]},
        )
    elif object_type == "process_template":
        response = api_client.post(
            "/api/v1/process-templates/beamline/amx",
            headers=idempotency_headers("process-template"),
            json={"name": "scan", "version": "1", "resource_slots": [], "steps": []},
        )
    else:
        template = api_client.post(
            "/api/v1/process-templates/beamline/amx",
            headers=idempotency_headers("process-template"),
            json={"name": "scan", "version": "1", "resource_slots": [], "steps": []},
        )
        response = api_client.post(
            "/api/v1/process-runs/beamline/amx",
            headers=idempotency_headers("process-run"),
            json={
                "name": "run",
                "description": "queued",
                "template_id": command_data(template)["id"],
                "steps": {},
            },
        )
    assert response.status_code == 201, response.text
    return response


@pytest.mark.parametrize(
    "object_type", ["resource", "resource_template", "process_template", "process_run"]
)
def test_lifecycle_route_dispatches_supported_object_types(
    api_client, idempotency_headers, create_namespace, object_type
):
    entity = _seed_lifecycle_entity(
        api_client, idempotency_headers, create_namespace, object_type
    )

    updated = api_client.post(
        f"/api/v1/lifecycle/{object_type}/{command_data(entity)['id']}",
        headers=idempotency_headers("lifecycle", **{"If-Match": '"1"'}),
        json={"status": "ACTIVE"},
    )

    assert updated.status_code == 200, updated.text
    assert command_data(updated)["status"] == "ACTIVE"
    assert updated.headers["ETag"] == '"2"'

    replayed = api_client.post(
        f"/api/v1/lifecycle/{object_type}/{command_data(entity)['id']}",
        headers=idempotency_headers("lifecycle", **{"If-Match": '"1"'}),
        json={"status": "ACTIVE"},
    )
    assert replayed.status_code == 200, replayed.text
    for field in ("id", "name", "description", "version", "status", "revision"):
        if field in command_data(updated):
            assert command_data(replayed)[field] == command_data(updated)[field]
    assert command_data(replayed)["revision"] == command_data(updated)["revision"] == 2
    assert replayed.headers["ETag"] == updated.headers["ETag"] == '"2"'

    archived = api_client.post(
        f"/api/v1/lifecycle/{object_type}/{command_data(entity)['id']}",
        headers=idempotency_headers("lifecycle-archive", **{"If-Match": '"2"'}),
        json={"status": "ARCHIVED"},
    )
    assert archived.status_code == 200, archived.text
    assert command_data(archived)["status"] == "ARCHIVED"
    assert command_data(archived)["revision"] == 3
    assert archived.headers["ETag"] == '"3"'
