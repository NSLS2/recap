import pytest
from fastapi.testclient import TestClient

from recap.server.app import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(
        create_app(tmp_path / "rest-resource.db", api_key="secret")
    ) as client:
        yield client


def draft(name="plate", version="1.0"):
    return {
        "name": name,
        "version": version,
        "type_names": ["container", "plate"],
        "property_groups": [
            {
                "name": "dimensions",
                "attributes": [{"name": "rows", "type": "int", "default": 8}],
            }
        ],
        "children": [{"name": "well", "version": "1.0", "type_names": ["well"]}],
    }


def headers(key):
    return {"Authorization": "Apikey secret", "Idempotency-Key": key}


def create_namespace(client):
    response = client.put(
        "/api/v1/namespaces/beamline/amx",
        headers=headers("namespace-1"),
        json={"metadata": {}},
    )
    assert response.status_code == 201


def test_post_and_patch_resource_template_aggregate(client):
    create_namespace(client)
    created = client.post(
        "/api/v1/resource-templates/beamline/amx",
        headers=headers("template-1"),
        json=draft(),
    )
    assert created.status_code == 201
    assert created.json()["children"]["well"]["types"][0]["name"] == "well"
    assert created.headers["ETag"] == '"1"'

    updated = client.patch(
        f"/api/v1/resource-templates/{created.json()['id']}",
        headers={**headers("template-2"), "If-Match": created.headers["ETag"]},
        json=draft("plate-updated"),
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "plate-updated"
    assert updated.headers["ETag"] == '"2"'


def test_resource_template_route_replays_and_has_no_granular_child_route(client):
    create_namespace(client)
    request = {"headers": headers("template-1"), "json": draft()}
    first = client.post("/api/v1/resource-templates/beamline/amx", **request)
    replay = client.post("/api/v1/resource-templates/beamline/amx", **request)
    assert replay.json() == first.json()
    assert (
        client.post(
            f"/api/v1/resource-templates/{first.json()['id']}/children",
            headers=headers("child-1"),
            json={},
        ).status_code
        == 422
    )
