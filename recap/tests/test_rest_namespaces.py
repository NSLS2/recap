from uuid import UUID

from recap.authentication.models import ProviderIdentity
from recap.authorization.policy import SnapshotNamespacePolicy
from recap.authorization.scopes import Scope
from recap.authorization.snapshot import (
    AuthorizationSnapshot,
    GrantProvenance,
    SnapshotMetadata,
)


def test_create_namespace_route(api_client, idempotency_headers):
    parent = api_client.put(
        "/api/v1/namespaces/beamline",
        headers=idempotency_headers("parent-1"),
        json={"metadata": {}},
    )
    assert parent.status_code == 201
    response = api_client.put(
        "/api/v1/namespaces/beamline/amx",
        headers=idempotency_headers("ns-1"),
        json={"metadata": {"beamline": "amx"}},
    )

    assert response.status_code == 201
    assert response.json()["path"] == "beamline/amx"
    assert response.json()["metadata"] == {"beamline": "amx"}
    assert response.headers["ETag"] == '"1"'
    UUID(response.json()["id"])


def test_patch_namespace_route_uses_if_match_and_returns_new_etag(
    api_client, idempotency_headers
):
    parent = api_client.put(
        "/api/v1/namespaces/beamline",
        headers=idempotency_headers("parent-1"),
        json={"metadata": {}},
    )
    assert parent.status_code == 201
    created = api_client.put(
        "/api/v1/namespaces/beamline/amx",
        headers=idempotency_headers("ns-1"),
        json={"metadata": {}},
    )

    response = api_client.patch(
        f"/api/v1/namespaces/{created.json()['id']}",
        headers=idempotency_headers("ns-2", **{"If-Match": created.headers["ETag"]}),
        json={"metadata": {"owner": "amx"}, "status": "ACTIVE"},
    )

    assert response.status_code == 200
    assert response.json()["revision"] == 2
    assert response.json()["status"] == "ACTIVE"
    assert response.headers["ETag"] == '"2"'


def test_replayed_put_returns_same_representation(api_client, idempotency_headers):
    parent = api_client.put(
        "/api/v1/namespaces/beamline",
        headers=idempotency_headers("parent-1"),
        json={"metadata": {}},
    )
    assert parent.status_code == 201
    request = {
        "headers": idempotency_headers("ns-1"),
        "json": {"metadata": {"beamline": "amx"}},
    }
    first = api_client.put("/api/v1/namespaces/beamline/amx", **request)
    replay = api_client.put("/api/v1/namespaces/beamline/amx", **request)

    assert replay.status_code == 201
    assert replay.json() == first.json()
    assert replay.headers["ETag"] == first.headers["ETag"]


def test_stale_if_match_maps_to_safe_conflict(api_client, idempotency_headers):
    parent = api_client.put(
        "/api/v1/namespaces/beamline",
        headers=idempotency_headers("parent-1"),
        json={"metadata": {}},
    )
    assert parent.status_code == 201
    created = api_client.put(
        "/api/v1/namespaces/beamline/amx",
        headers=idempotency_headers("ns-1"),
        json={"metadata": {}},
    )

    response = api_client.patch(
        f"/api/v1/namespaces/{created.json()['id']}",
        headers=idempotency_headers("ns-2", **{"If-Match": '"2"'}),
        json={"metadata": {"wrong": True}},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert "revision" not in response.text.lower()


def test_missing_idempotency_key_and_if_match_are_validation_errors(
    api_client, auth_header, idempotency_headers
):
    put = api_client.put(
        "/api/v1/namespaces/beamline/amx",
        headers=auth_header,
        json={"metadata": {}},
    )
    patch = api_client.patch(
        "/api/v1/namespaces/00000000-0000-0000-0000-000000000000",
        headers=idempotency_headers("ns-2"),
        json={"metadata": {}},
    )

    assert put.status_code == 422
    assert patch.status_code == 422


def test_namespace_routes_require_authentication(api_client):
    response = api_client.put(
        "/api/v1/namespaces/beamline/amx",
        headers={"Idempotency-Key": "ns-1"},
        json={"metadata": {}},
    )

    assert response.status_code == 401


def test_nested_namespace_requires_explicit_parent(api_client, idempotency_headers):
    response = api_client.put(
        "/api/v1/namespaces/beamline/amx",
        headers=idempotency_headers("nested-1"),
        json={"metadata": {}},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    message = response.json()["error"]["message"]
    assert message == (
        "Parent namespace must be explicitly created before "
        "creating a nested namespace."
    )
    assert "beamline" not in message


def test_list_namespace_children_route_returns_relative_direct_children(
    api_client, auth_header, idempotency_headers
):
    for path, key in (("beamlines", "ns-1"), ("beamlines/amx", "ns-2"),
                      ("beamlines/amx/proposals", "ns-3")):
        response = api_client.put(
            f"/api/v1/namespaces/{path}",
            headers=idempotency_headers(key),
            json={"metadata": {}},
        )
        assert response.status_code == 201

    response = api_client.get("/api/v1/namespaces/children/beamlines", headers=auth_header)

    assert response.status_code == 200
    assert response.json() == ["amx"]


def test_list_namespace_children_root_route_returns_top_level_namespaces(
    api_client, auth_header, idempotency_headers
):
    for path, key in (("beamlines", "ns-1"), ("staff", "ns-2")):
        response = api_client.put(
            f"/api/v1/namespaces/{path}",
            headers=idempotency_headers(key),
            json={"metadata": {}},
        )
        assert response.status_code == 201

    response = api_client.get("/api/v1/namespaces/children", headers=auth_header)

    assert response.status_code == 200
    assert sorted(response.json()) == ["beamlines", "staff"]


def test_list_namespace_children_route_requires_authentication(api_client):
    response = api_client.get("/api/v1/namespaces/children")

    assert response.status_code == 401


def test_list_namespace_children_only_returns_paths_to_authorized_descendant(
    api_client, auth_header, idempotency_headers
):
    app = api_client.app
    for path, key in (
        ("beamlines", "ns-1"),
        ("beamlines/amx", "ns-2"),
        ("beamlines/amx/proposals", "ns-3"),
        ("beamlines/fmx", "ns-4"),
        ("staff", "ns-5"),
    ):
        response = api_client.put(
            f"/api/v1/namespaces/{path}",
            headers=idempotency_headers(key),
            json={"metadata": {}},
        )
        assert response.status_code == 201

    identity = ProviderIdentity(provider="api-key", subject="single-user")
    app.state.namespace_policy = SnapshotNamespacePolicy(
        AuthorizationSnapshot(
            metadata=SnapshotMetadata(format_version=1, source_revision="test"),
            grants=frozenset(
                {
                    GrantProvenance(
                        identity=identity,
                        namespace_path="beamlines/amx/proposals",
                        scope=Scope.NAMESPACE_READ,
                        group="scientists",
                        role="member",
                    )
                }
            ),
        )
    )

    root = api_client.get("/api/v1/namespaces/children", headers=auth_header)
    beamlines = api_client.get(
        "/api/v1/namespaces/children/beamlines", headers=auth_header
    )

    assert root.status_code == 200
    assert root.json() == ["beamlines"]
    assert beamlines.status_code == 200
    assert beamlines.json() == ["amx"]
