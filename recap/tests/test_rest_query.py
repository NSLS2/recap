from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from recap.adapter.schema_registry import QUERY_SCHEMA_KEYS, schema_for
from recap.adapter.transport import QueryRequest, QueryResult
from recap.authentication.models import ProviderIdentity
from recap.authorization.policy import SnapshotNamespacePolicy
from recap.authorization.scopes import Scope
from recap.authorization.snapshot import (
    AuthorizationSnapshot,
    GrantProvenance,
    SnapshotMetadata,
)
from recap.dsl.query import QuerySpec
from recap.exceptions import RecapProtocolError, RecapValidationError
from recap.schemas.namespace import NamespaceRef
from recap.schemas.resource import ResourceRef, ResourceSchema
from recap.server.query_service import QueryService


def test_rest_query_returns_transport_result(
    integration_database_path, query_resource_tree_path, auth_header
):
    from fastapi.testclient import TestClient

    from recap.server.app import create_app

    with TestClient(create_app(integration_database_path, api_key="secret")) as client:
        response = client.post(
            "/api/v1/query",
            headers=auth_header,
            json={
                "entity": "resource",
                "projection": "full",
                "namespace_path": query_resource_tree_path,
                "spec": {"filters": {}, "limit": 1},
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["entity"] == "resource"
    assert len(body["items"]) == 1


def test_rest_namespace_ref_query_hydrates_namespace_refs(
    integration_database_path, query_namespace_path, auth_header
):
    from fastapi.testclient import TestClient

    from recap.server.app import create_app

    with TestClient(create_app(integration_database_path, api_key="secret")) as client:
        response = client.post(
            "/api/v1/query",
            headers=auth_header,
            json={
                "entity": "namespace",
                "projection": "ref",
                "namespace_path": query_namespace_path,
                "spec": {"filters": {}, "include_mutable": True},
            },
        )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert set(item) == {"id", "path"}
    assert NamespaceRef.model_validate(item).path == query_namespace_path


def test_rest_count_permissions_and_namespace_context(
    integration_database_path, query_namespace_path, auth_header
):
    from fastapi.testclient import TestClient

    from recap.server.app import create_app

    with TestClient(create_app(integration_database_path, api_key="secret")) as client:
        count = client.post(
            "/api/v1/query/count",
            headers=auth_header,
            json={
                "entity": "namespace",
                "projection": "full",
                "namespace_path": query_namespace_path,
                "spec": {"filters": {}, "include_mutable": True},
            },
        )
        permissions = client.get(
            "/api/v1/permissions",
            headers=auth_header,
            params={"namespace_path": query_namespace_path},
        )
        context = client.get(
            f"/api/v1/namespaces/context/{query_namespace_path}",
            headers=auth_header,
        )

    assert count.status_code == 200
    assert count.json() == 1
    assert permissions.status_code == 200
    assert permissions.json()["snapshot_generation"] is None
    assert permissions.json()["effective_scopes"]
    assert context.status_code == 200
    assert context.json()["id"]
    assert context.json()["revision"] == 1
    assert context.headers["ETag"] == '"1"'


@pytest.mark.parametrize(
    "request_call",
    [
        lambda client, headers: client.post(
            "/api/v1/query",
            headers=headers,
            json={
                "entity": "namespace",
                "projection": "full",
                "namespace_path": "beamline//amx",
                "spec": {},
            },
        ),
        lambda client, headers: client.post(
            "/api/v1/query/count",
            headers=headers,
            json={
                "entity": "namespace",
                "projection": "full",
                "namespace_path": "beamline//amx",
                "spec": {},
            },
        ),
        lambda client, headers: client.get(
            "/api/v1/permissions",
            headers=headers,
            params={"namespace_path": "beamline//amx"},
        ),
        lambda client, headers: client.get(
            "/api/v1/namespaces/context/beamline//amx",
            headers=headers,
        ),
    ],
    ids=("query", "count", "permissions", "context"),
)
def test_rest_namespace_routes_reject_noncanonical_paths(api_client, auth_header, request_call):
    response = request_call(api_client, auth_header)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.json()["error"]["message"] == "Request validation failed"


def test_rest_namespace_context_conceals_undiscoverable_namespace(
    api_client, auth_header, idempotency_headers
):
    created = api_client.put(
        "/api/v1/namespaces/private",
        headers=idempotency_headers("private-1"),
        json={"metadata": {}},
    )
    assert created.status_code == 201

    identity = ProviderIdentity(provider="api-key", subject="single-user")
    api_client.app.state.namespace_policy = SnapshotNamespacePolicy(
        AuthorizationSnapshot(
            metadata=SnapshotMetadata(format_version=1, source_revision="test"),
            grants=frozenset(
                {
                    GrantProvenance(
                        identity=identity,
                        namespace_path="public",
                        scope=Scope.NAMESPACE_READ,
                        group="scientists",
                        role="member",
                    )
                }
            ),
        )
    )

    response = api_client.get(
        "/api/v1/namespaces/context/private",
        headers={**auth_header, "X-Request-ID": "caller-id"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert response.headers["X-Request-ID"] != "caller-id"
    assert response.headers["X-Request-ID"] == response.json()["error"]["request_id"]
    assert "private" not in response.text


def test_rest_query_rejects_unknown_entity_and_invalid_spec(api_client, auth_header):
    for payload in (
        {
            "entity": "unknown",
            "projection": "full",
            "namespace_path": "test",
            "spec": {},
        },
        {
            "entity": "namespace",
            "projection": "full",
            "namespace_path": "test",
            "spec": {"unknown": True},
        },
    ):
        response = api_client.post("/api/v1/query", headers=auth_header, json=payload)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize(
    "spec",
    [
        {"filters": {"missing": "value"}},
        {"filters": {"template__missing": "value"}},
        {"predicates": [{"field": "missing", "op": "eq", "value": "value"}]},
        {"predicates": [{"field": "template.missing", "op": "eq", "value": "value"}]},
        {"orderings": [{"field": "missing", "direction": "asc"}]},
        {"orderings": [{"field": "template.missing", "direction": "desc"}]},
    ],
)
def test_rest_query_rejects_malformed_query_paths(
    api_client, auth_header, create_namespace, spec
):
    create_namespace("test")
    response = api_client.post(
        "/api/v1/query",
        headers=auth_header,
        json={
            "entity": "resource",
            "projection": "full",
            "namespace_path": "test",
            "spec": spec,
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_rest_query_requires_authentication(api_client):
    response = api_client.post(
        "/api/v1/query",
        json={
            "entity": "namespace",
            "projection": "full",
            "namespace_path": "test",
            "spec": {},
        },
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert response.headers["X-Request-ID"] == response.json()["error"]["request_id"]


@pytest.mark.parametrize(
    "request_call",
    [
        lambda client: client.post(
            "/api/v1/query/count",
            json={
                "entity": "namespace",
                "projection": "full",
                "namespace_path": "test",
                "spec": {},
            },
        ),
        lambda client: client.get("/api/v1/permissions", params={"namespace_path": "test"}),
        lambda client: client.get("/api/v1/namespaces/context/test"),
    ],
    ids=("count", "permissions", "namespace-context"),
)
def test_rest_security_routes_require_authentication_and_request_id(request_call, api_client):
    response = request_call(api_client)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert response.headers["X-Request-ID"] == response.json()["error"]["request_id"]


@pytest.mark.parametrize(
    "request_call",
    [
        lambda client, headers: client.post(
            "/api/v1/query/count",
            headers=headers,
            json={
                "entity": "namespace",
                "projection": "full",
                "namespace_path": "missing",
                "spec": {},
            },
        ),
        lambda client, headers: client.get(
            "/api/v1/permissions",
            headers=headers,
            params={"namespace_path": "missing"},
        ),
        lambda client, headers: client.get(
            "/api/v1/namespaces/context/missing", headers=headers
        ),
    ],
    ids=("count", "permissions", "namespace-context"),
)
def test_rest_security_routes_replace_caller_request_id(request_call, api_client, auth_header):
    response = request_call(api_client, {**auth_header, "X-Request-ID": "caller-id"})

    assert response.headers["X-Request-ID"] != "caller-id"
    if response.status_code != 200:
        assert response.headers["X-Request-ID"] == response.json()["error"]["request_id"]


def test_rest_query_request_id_is_server_generated(api_client, auth_header):
    response = api_client.post(
        "/api/v1/query",
        headers={**auth_header, "X-Request-ID": "caller-id"},
        json={
            "entity": "namespace",
            "projection": "full",
            "namespace_path": "test",
            "spec": {},
        },
    )

    assert response.status_code == 404
    assert response.headers["X-Request-ID"] != "caller-id"
    assert response.headers["X-Request-ID"] == response.json()["error"]["request_id"]


def test_query_request_uses_stable_entity_key():
    request = QueryRequest.from_query(
        ResourceSchema,
        QuerySpec(filters={"name": "sample"}),
        namespace_path="beamline/amx",
    )

    assert request.entity == "resource"
    assert request.projection == "full"
    assert request.namespace_path == "beamline/amx"
    assert request.spec["filters"] == {"name": "sample"}


def test_canonical_alias_query_uses_stable_entity_and_projection():
    request = QueryRequest.from_query(
        ResourceRef, QuerySpec(), namespace_path="beamline/amx"
    )

    assert request.model_dump(mode="json") == {
        "entity": "resource",
        "projection": "full",
        "namespace_path": "beamline/amx",
        "spec": request.spec,
    }
    assert "schema_name" not in request.model_dump()


@pytest.mark.parametrize(
    "payload",
    [
        {"entity": "unknown", "projection": "full", "namespace_path": "x", "spec": {}},
        {"entity": "resource", "projection": "other", "namespace_path": "x", "spec": {}},
    ],
)
def test_query_request_rejects_unknown_wire_keys(payload):
    with pytest.raises(ValidationError):
        QueryRequest.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"filters": {}, "unknown": True},
        {"predicates": [{"field": "name", "op": "eq", "value": "x", "unknown": True}]},
        {"orderings": [{"field": "name", "direction": "asc", "unknown": True}]},
    ],
)
def test_query_spec_rejects_unknown_wire_keys(payload):
    with pytest.raises(ValidationError):
        QuerySpec.model_validate(payload)


def test_query_result_rejects_malformed_items_and_schema_mismatch():
    from recap.adapter.transport import hydrate_result

    with pytest.raises(ValidationError):
        QueryResult.model_validate(
            {"entity": "resource", "projection": "full", "items": ["not an object"]}
        )

    result = QueryResult(entity="process_run", projection="full", items=[])
    with pytest.raises(RecapProtocolError):
        hydrate_result(ResourceSchema, result)


def test_stable_query_result_requires_projection():
    with pytest.raises(ValidationError):
        QueryResult.model_validate({"entity": "resource", "items": []})


def test_query_result_rejects_malformed_entity_with_legacy_schema_name():
    with pytest.raises(ValidationError):
        QueryResult.model_validate(
            {
                "entity": "unknown",
                "projection": "full",
                "schema_name": "ResourceSchema",
                "items": [],
            }
        )


def test_legacy_query_result_schema_name_defaults_projection():
    result = QueryResult.model_validate({"schema_name": "ResourceRef", "items": []})

    assert result.entity == "resource"
    assert result.projection == "ref"
    assert result.projection == "ref"


def test_schema_registry_keeps_full_and_ref_projections_distinct():
    assert schema_for("resource", "full") is ResourceSchema
    assert schema_for("resource", "ref") is ResourceRef
    assert QUERY_SCHEMA_KEYS[("resource", "full")] is ResourceSchema
    assert QUERY_SCHEMA_KEYS[("resource", "ref")] is ResourceRef


def test_query_service_validates_and_executes_authorized_query():
    from unittest.mock import Mock

    backend = Mock()
    backend.query_authorized.return_value = []
    policy = Mock()
    actor = object()
    request = QueryRequest(
        entity="resource",
        projection="full",
        namespace_path="beamline/amx",
        spec={"filters": {"id": str(uuid4())}, "include_mutable": True},
    )

    QueryService(backend).query(request, actor=actor, policy=policy)

    backend.query_authorized.assert_called_once()
    assert backend.query_authorized.call_args.args[0] is ResourceSchema


def test_query_service_rejects_invalid_spec():
    from unittest.mock import Mock

    request = QueryRequest(
        entity="resource", projection="full", namespace_path="beamline", spec={"unknown": True}
    )

    with pytest.raises(RecapValidationError):
        QueryService(Mock()).query(request, actor=object(), policy=Mock())


def test_query_service_rejects_backend_query_validation_before_execution():
    from unittest.mock import Mock

    backend = Mock()
    backend.validate_query.side_effect = ValueError("invalid field path")
    request = QueryRequest(
        entity="resource",
        projection="full",
        namespace_path="beamline",
        spec={},
    )

    with pytest.raises(RecapValidationError):
        QueryService(backend).query(request, actor=object(), policy=Mock())

    backend.query.assert_not_called()
    backend.query_authorized.assert_not_called()


def test_query_service_rejects_empty_body_and_non_integer_count():
    from recap.server.query_service import QueryService

    service = QueryService(SimpleNamespace())
    with pytest.raises(RecapProtocolError):
        service.parse_result(None)
    with pytest.raises(RecapProtocolError):
        service.parse_count(True)
