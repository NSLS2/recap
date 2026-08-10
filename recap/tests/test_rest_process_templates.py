from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from recap.server.app import create_app


@pytest.fixture
def auth_header():
    return {"Authorization": "Apikey secret"}


@pytest.fixture
def client(tmp_path):
    with TestClient(
        create_app(tmp_path / "rest-process.db", api_key="secret")
    ) as client:
        yield client


def draft(labels=None):
    return {
        "name": "screening",
        "version": "1.0",
        "labels": labels or ["mx"],
        "resource_slots": [
            {
                "name": "sample",
                "resource_type": "container",
                "direction": "input",
                "create_resource_type": True,
            }
        ],
        "steps": [
            {
                "name": "collect",
                "role_bindings": {"source": "sample"},
                "parameter_groups": [
                    {
                        "name": "exposure",
                        "attributes": [
                            {
                                "name": "duration",
                                "type": "float",
                                "unit": "s",
                                "default": 0.1,
                            }
                        ],
                    }
                ],
            }
        ],
    }


def create_namespace(client, auth_header):
    parent = client.put(
        "/api/v1/namespaces/beamline",
        headers={**auth_header, "Idempotency-Key": "parent-1"},
        json={"metadata": {}},
    )
    assert parent.status_code == 201
    response = client.put(
        "/api/v1/namespaces/beamline/amx",
        headers={**auth_header, "Idempotency-Key": "namespace-1"},
        json={"metadata": {}},
    )
    assert response.status_code == 201


def test_post_and_patch_process_template_routes(client, auth_header):
    create_namespace(client, auth_header)
    created = client.post(
        "/api/v1/process-templates/beamline/amx",
        headers={**auth_header, "Idempotency-Key": "template-1"},
        json=draft(),
    )

    assert created.status_code == 201
    UUID(created.json()["id"])
    assert (
        created.json()["step_templates"]["collect"]["resource_slots"]["source"]["name"]
        == "sample"
    )
    assert created.headers["ETag"] == '"1"'

    updated = client.patch(
        f"/api/v1/process-templates/{created.json()['id']}",
        headers={
            **auth_header,
            "Idempotency-Key": "template-2",
            "If-Match": created.headers["ETag"],
        },
        json=draft(["updated"]),
    )
    assert updated.status_code == 200
    assert updated.json()["labels"] == ["updated"]
    assert updated.headers["ETag"] == '"2"'


def test_process_template_route_replay_and_validation(client, auth_header):
    create_namespace(client, auth_header)
    request = {
        "headers": {**auth_header, "Idempotency-Key": "template-1"},
        "json": draft(),
    }
    first = client.post("/api/v1/process-templates/beamline/amx", **request)
    replay = client.post("/api/v1/process-templates/beamline/amx", **request)
    invalid = draft()
    invalid["steps"][0]["role_bindings"] = {"source": "missing"}
    rejected = client.post(
        "/api/v1/process-templates/beamline/amx",
        headers={**auth_header, "Idempotency-Key": "template-2"},
        json=invalid,
    )

    assert replay.json() == first.json()
    assert rejected.status_code == 422
