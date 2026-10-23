from dataclasses import replace

from recap.commands.registry import CommandRegistration
from recap.server.rest import command_registration, router

from .command_responses import command_data


def test_resource_routes_are_canonical(api_client):
    paths = {route.path for route in router.routes}
    assert "/api/v1/resources/{namespace_path:path}" in paths
    assert "/api/v1/resources/{resource_id}" in paths
    assert "/api/v1/resources/{source_resource_id}/copies" in paths


def test_registry_dependency_resolves_named_command():
    resolved = command_registration("update_resource")()

    assert isinstance(resolved, CommandRegistration)
    assert resolved.name == "update_resource"


def test_create_and_patch_resource(api_client, idempotency_headers, monkeypatch):
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
        json={"name": "plate-1", "template_id": command_data(template)["id"]},
    )
    assert resource.status_code == 201
    assert command_data(resource)["name"] == "plate-1"
    from recap.commands.registry import CommandRegistry

    decoded = []
    original_by_name = CommandRegistry.by_name

    def by_name(registry, name):
        registration = original_by_name(registry, name)
        if name == "update_resource":
            decoder = registration.decode_command

            def decode(path_params, headers, body):
                command = decoder(path_params, headers, body)
                decoded.append(command)
                return command

            return replace(registration, decode_command=decode)
        return registration

    monkeypatch.setattr(CommandRegistry, "by_name", by_name)
    updated = api_client.patch(
        f"/api/v1/resources/{command_data(resource)['id']}",
        headers=idempotency_headers(
            "resource-2", **{"If-Match": resource.headers["ETag"]}
        ),
        json={"name": "plate-2"},
    )
    assert updated.status_code == 200
    assert command_data(updated)["name"] == "plate-2"
    assert updated.request.content == b'{"name":"plate-2"}'
    assert "expected_revision" not in updated.request.content.decode()
    assert updated.headers["ETag"] == '"2"'
    assert str(decoded[0].resource_id) == command_data(resource)["id"]
    assert decoded[0].expected_revision == 1


def test_create_resource_replays_sequentially_with_same_idempotency_key(
    api_client, idempotency_headers
):
    namespace = api_client.put(
        "/api/v1/namespaces/beamline",
        headers=idempotency_headers("namespace-replay"),
        json={"metadata": {}},
    )
    assert namespace.status_code == 201
    template = api_client.post(
        "/api/v1/resource-templates/beamline",
        headers=idempotency_headers("template-replay"),
        json={"name": "plate", "version": "1", "type_names": []},
    )
    assert template.status_code == 201

    headers = idempotency_headers("resource-replay")
    payload = {"name": "plate-1", "template_id": command_data(template)["id"]}
    first = api_client.post("/api/v1/resources/beamline", headers=headers, json=payload)
    replay = api_client.post(
        "/api/v1/resources/beamline", headers=headers, json=payload
    )

    assert first.status_code == replay.status_code == 201
    first_body = command_data(first)
    replay_body = command_data(replay)
    assert first_body["id"] == replay_body["id"]
    assert first_body["name"] == replay_body["name"] == "plate-1"
    assert first_body["revision"] == replay_body["revision"] == 1
    assert first_body["template"]["id"] == replay_body["template"]["id"]


def test_create_resource_response_preserves_dynamic_properties(
    api_client, idempotency_headers, create_namespace
):
    create_namespace("beamline")
    template = api_client.post(
        "/api/v1/resource-templates/beamline",
        headers=idempotency_headers("template-properties"),
        json={
            "name": "sample",
            "version": "1",
            "type_names": [],
            "property_groups": [
                {
                    "name": "measurements",
                    "attributes": [{"name": "mass", "type": "float", "default": 0.0}],
                }
            ],
        },
    )
    assert template.status_code == 201

    request = {
        "headers": idempotency_headers("resource-properties"),
        "json": {
            "name": "sample-1",
            "template_id": command_data(template)["id"],
            "properties": {"measurements": {"mass": 2.5}},
        },
    }
    first = api_client.post("/api/v1/resources/beamline", **request)
    assert first.status_code == 201
    assert (
        command_data(first)["properties"]["measurements"]["values"]["mass"]["value"]
        == 2.5
    )


def test_copy_resource_uses_destination_namespace_in_body(
    api_client, idempotency_headers
):
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
        json={"name": "plate-1", "template_id": command_data(template)["id"]},
    )

    copied = api_client.post(
        f"/api/v1/resources/{command_data(resource)['id']}/copies",
        headers=idempotency_headers("copy-1"),
        json={"destination_namespace": "beamline", "name": "plate-copy"},
    )

    assert copied.status_code == 201
    assert command_data(copied)["name"] == "plate-copy"


def test_copy_resource_accepts_parent_id(api_client, idempotency_headers):
    api_client.put(
        "/api/v1/namespaces/beamline",
        headers=idempotency_headers("copy-child-namespace"),
        json={"metadata": {}},
    )
    template = api_client.post(
        "/api/v1/resource-templates/beamline",
        headers=idempotency_headers("copy-child-template"),
        json={"name": "plate", "version": "1", "type_names": []},
    )
    parent = api_client.post(
        "/api/v1/resources/beamline",
        headers=idempotency_headers("copy-child-parent"),
        json={"name": "group", "template_id": command_data(template)["id"]},
    )
    source = api_client.post(
        "/api/v1/resources/beamline",
        headers=idempotency_headers("copy-child-source"),
        json={"name": "source", "template_id": command_data(template)["id"]},
    )
    copied = api_client.post(
        f"/api/v1/resources/{command_data(source)['id']}/copies",
        headers=idempotency_headers("copy-as-child"),
        json={
            "destination_namespace": "beamline",
            "parent_id": command_data(parent)["id"],
        },
    )

    assert copied.status_code == 201
    assert command_data(copied)["copied_from_id"] == command_data(source)["id"]


def test_copy_resource_requires_destination_namespace(api_client, idempotency_headers):
    response = api_client.post(
        "/api/v1/resources/00000000-0000-0000-0000-000000000000/copies",
        headers=idempotency_headers("copy-missing-destination"),
        json={},
    )

    assert response.status_code == 422


def test_patch_active_resource_requires_in_place(
    api_client, idempotency_headers, create_namespace
):
    create_namespace("beamline")
    template = api_client.post(
        "/api/v1/resource-templates/beamline",
        headers=idempotency_headers("in-place-template"),
        json={"name": "plate", "version": "1", "type_names": []},
    )
    resource = api_client.post(
        "/api/v1/resources/beamline",
        headers=idempotency_headers("in-place-resource"),
        json={"name": "plate-1", "template_id": command_data(template)["id"]},
    )
    resource_id = command_data(resource)["id"]
    activated = api_client.post(
        f"/api/v1/lifecycle/resource/{resource_id}",
        headers=idempotency_headers("in-place-activate", **{"If-Match": '"1"'}),
        json={"status": "ACTIVE"},
    )
    assert activated.status_code == 200, activated.text
    assert activated.headers["ETag"] == '"2"'

    rejected = api_client.patch(
        f"/api/v1/resources/{resource_id}",
        headers=idempotency_headers("in-place-reject", **{"If-Match": '"2"'}),
        json={"name": "plate-fixed"},
    )
    assert rejected.status_code == 409, rejected.text

    updated = api_client.patch(
        f"/api/v1/resources/{resource_id}",
        headers=idempotency_headers("in-place-accept", **{"If-Match": '"2"'}),
        json={"name": "plate-fixed", "in_place": True},
    )
    assert updated.status_code == 200, updated.text
    assert command_data(updated)["id"] == resource_id
    assert command_data(updated)["name"] == "plate-fixed"
    assert command_data(updated)["status"] == "ACTIVE"
    assert updated.headers["ETag"] == '"3"'
