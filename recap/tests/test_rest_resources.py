from recap.server.rest import router


def test_resource_routes_are_canonical(api_client):
    paths = {route.path for route in router.routes}
    assert "/api/v1/resources/{namespace_path:path}" in paths
    assert "/api/v1/resources/{resource_id}" in paths
    assert "/api/v1/resources/{source_resource_id}/copies" in paths


def test_create_and_patch_resource(api_client, idempotency_headers):
    namespace = api_client.put(
        "/api/v1/namespaces/beamline",
        headers=idempotency_headers("namespace-1"),
        json={"metadata": {}},
    )
    assert namespace.status_code == 201
    template = api_client.post(
        "/api/v1/resource-templates/beamline",
        headers=idempotency_headers("template-1"),
        json={
            "name": "plate",
            "version": "1",
            "type_names": [],
            "property_groups": [],
            "children": [],
        },
    )
    assert template.status_code == 201
    resource = api_client.post(
        "/api/v1/resources/beamline",
        headers=idempotency_headers("resource-1"),
        json={"name": "plate-1", "template_id": template.json()["id"]},
    )
    assert resource.status_code == 201
    assert resource.json()["name"] == "plate-1"
    updated = api_client.patch(
        f"/api/v1/resources/{resource.json()['id']}",
        headers=idempotency_headers("resource-2", **{"If-Match": resource.headers["ETag"]}),
        json={"name": "plate-2"},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "plate-2"


def test_copy_resource_uses_destination_namespace_in_body(api_client, idempotency_headers):
    namespace = api_client.put(
        "/api/v1/namespaces/beamline",
        headers=idempotency_headers("namespace-copy"),
        json={"metadata": {}},
    )
    assert namespace.status_code == 201
    template = api_client.post(
        "/api/v1/resource-templates/beamline",
        headers=idempotency_headers("template-copy"),
        json={"name": "plate", "version": "1", "type_names": []},
    )
    resource = api_client.post(
        "/api/v1/resources/beamline",
        headers=idempotency_headers("resource-copy"),
        json={"name": "plate-1", "template_id": template.json()["id"]},
    )

    copied = api_client.post(
        f"/api/v1/resources/{resource.json()['id']}/copies",
        headers=idempotency_headers("copy-1"),
        json={"destination_namespace": "beamline", "name": "plate-copy"},
    )

    assert copied.status_code == 201
    assert copied.json()["name"] == "plate-copy"


def test_copy_resource_requires_destination_namespace(api_client, idempotency_headers):
    response = api_client.post(
        "/api/v1/resources/00000000-0000-0000-0000-000000000000/copies",
        headers=idempotency_headers("copy-missing-destination"),
        json={},
    )

    assert response.status_code == 422
