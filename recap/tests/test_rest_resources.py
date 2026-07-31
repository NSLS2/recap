import pytest
from fastapi.testclient import TestClient

from recap.server.app import create_app
from recap.server.rest import router


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "rest-resource.db", api_key="secret")) as client:
        yield client


def headers(key):
    return {"Authorization": "Apikey secret", "Idempotency-Key": key}


def test_resource_routes_are_canonical(client):
    paths = {route.path for route in router.routes}
    assert "/api/v1/resources/{namespace_path:path}" in paths
    assert "/api/v1/resources/{resource_id}" in paths
    assert "/api/v1/resources/{source_resource_id}/copies/{destination_namespace_path:path}" in paths


def test_create_and_patch_resource(client):
    namespace = client.put(
        "/api/v1/namespaces/beamline",
        headers=headers("namespace-1"),
        json={"metadata": {}},
    )
    assert namespace.status_code == 201
    template = client.post(
        "/api/v1/namespaces/beamline/resource-templates",
        headers=headers("template-1"),
        json={"name": "plate", "version": "1", "type_names": [], "property_groups": [], "children": []},
    )
    assert template.status_code == 201
    resource = client.post(
        "/api/v1/resources/beamline",
        headers=headers("resource-1"),
        json={"name": "plate-1", "template_id": template.json()["id"]},
    )
    assert resource.status_code == 201
    assert resource.json()["name"] == "plate-1"
    updated = client.patch(
        f"/api/v1/resources/{resource.json()['id']}",
        headers={**headers("resource-2"), "If-Match": resource.headers["ETag"]},
        json={"name": "plate-2"},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "plate-2"
