from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from recap.server.app import create_app


@pytest.fixture
def auth_header():
    return {"Authorization": "Apikey secret"}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "rest.db", api_key="secret")) as client:
        yield client


def test_create_namespace_route(client, auth_header):
    response = client.put(
        "/api/v1/namespaces/beamline/amx",
        headers={**auth_header, "Idempotency-Key": "ns-1"},
        json={"metadata": {"beamline": "amx"}},
    )

    assert response.status_code == 201
    assert response.json()["path"] == "beamline/amx"
    assert response.json()["metadata"] == {"beamline": "amx"}
    assert response.headers["ETag"] == '"1"'
    UUID(response.json()["id"])


def test_patch_namespace_route_uses_if_match_and_returns_new_etag(client, auth_header):
    created = client.put(
        "/api/v1/namespaces/beamline/amx",
        headers={**auth_header, "Idempotency-Key": "ns-1"},
        json={"metadata": {}},
    )

    response = client.patch(
        f"/api/v1/namespaces/{created.json()['id']}",
        headers={
            **auth_header,
            "Idempotency-Key": "ns-2",
            "If-Match": created.headers["ETag"],
        },
        json={"metadata": {"owner": "amx"}, "status": "ACTIVE"},
    )

    assert response.status_code == 200
    assert response.json()["revision"] == 2
    assert response.json()["status"] == "ACTIVE"
    assert response.headers["ETag"] == '"2"'


def test_replayed_put_returns_same_representation(client, auth_header):
    request = {
        "headers": {**auth_header, "Idempotency-Key": "ns-1"},
        "json": {"metadata": {"beamline": "amx"}},
    }
    first = client.put("/api/v1/namespaces/beamline/amx", **request)
    replay = client.put("/api/v1/namespaces/beamline/amx", **request)

    assert replay.status_code == 201
    assert replay.json() == first.json()
    assert replay.headers["ETag"] == first.headers["ETag"]


def test_stale_if_match_maps_to_safe_conflict(client, auth_header):
    created = client.put(
        "/api/v1/namespaces/beamline/amx",
        headers={**auth_header, "Idempotency-Key": "ns-1"},
        json={"metadata": {}},
    )

    response = client.patch(
        f"/api/v1/namespaces/{created.json()['id']}",
        headers={
            **auth_header,
            "Idempotency-Key": "ns-2",
            "If-Match": '"2"',
        },
        json={"metadata": {"wrong": True}},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert "revision" not in response.text.lower()


def test_missing_idempotency_key_and_if_match_are_validation_errors(client, auth_header):
    put = client.put(
        "/api/v1/namespaces/beamline/amx",
        headers=auth_header,
        json={"metadata": {}},
    )
    patch = client.patch(
        "/api/v1/namespaces/00000000-0000-0000-0000-000000000000",
        headers={**auth_header, "Idempotency-Key": "ns-2"},
        json={"metadata": {}},
    )

    assert put.status_code == 422
    assert patch.status_code == 422


def test_namespace_routes_require_authentication(client):
    response = client.put(
        "/api/v1/namespaces/beamline/amx",
        headers={"Idempotency-Key": "ns-1"},
        json={"metadata": {}},
    )

    assert response.status_code == 401
