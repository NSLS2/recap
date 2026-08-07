import pytest
from fastapi.testclient import TestClient

from recap.server.app import create_app
from recap.server.rest import router


@pytest.fixture
def client(tmp_path):
    with TestClient(
        create_app(tmp_path / "rest-resource.db", api_key="secret")
    ) as client:
        yield client


def headers(key):
    return {"Authorization": "Apikey secret", "Idempotency-Key": key}


def test_resource_routes_are_canonical(client):
    paths = {route.path for route in router.routes}
    assert "/api/v1/resources/{namespace_path:path}" in paths
    assert "/api/v1/resources/{resource_id}" in paths
    assert "/api/v1/resources/{source_resource_id}/copies" in paths


def test_create_and_patch_resource(client):
    namespace = client.put(
        "/api/v1/namespaces/beamline",
        headers=headers("namespace-1"),
        json={"metadata": {}},
    )
    assert namespace.status_code == 201
    template = client.post(
        "/api/v1/resource-templates/beamline",
        headers=headers("template-1"),
        json={
            "name": "plate",
            "version": "1",
            "type_names": [],
            "property_groups": [],
            "children": [],
        },
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


def test_copy_resource_uses_destination_namespace_in_body(client):
    namespace = client.put(
        "/api/v1/namespaces/beamline",
        headers=headers("namespace-copy"),
        json={"metadata": {}},
    )
    assert namespace.status_code == 201
    template = client.post(
        "/api/v1/resource-templates/beamline",
        headers=headers("template-copy"),
        json={"name": "plate", "version": "1", "type_names": []},
    )
    resource = client.post(
        "/api/v1/resources/beamline",
        headers=headers("resource-copy"),
        json={"name": "plate-1", "template_id": template.json()["id"]},
    )

    copied = client.post(
        f"/api/v1/resources/{resource.json()['id']}/copies",
        headers=headers("copy-1"),
        json={"destination_namespace": "beamline", "name": "plate-copy"},
    )

    assert copied.status_code == 201
    assert copied.json()["name"] == "plate-copy"


def test_copy_resource_requires_destination_namespace(client):
    response = client.post(
        "/api/v1/resources/00000000-0000-0000-0000-000000000000/copies",
        headers=headers("copy-missing-destination"),
        json={},
    )

    assert response.status_code == 422
