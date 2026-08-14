from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from recap.adapter.http_transport import HTTPResult, HTTPTransport
from recap.adapter.transport import serialize_model
from recap.db.process import ProcessRun
from recap.dsl.query import QuerySpec
from recap.exceptions import (
    RecapAuthenticationError,
    RecapConflictError,
    RecapInternalError,
    RecapNotFoundError,
    RecapPermissionDeniedError,
    RecapProtocolError,
    RecapRequestError,
    RecapServiceUnavailableError,
    RecapValidationError,
)
from recap.schemas.resource import ResourceSchema
from recap.tests.transport_factories import full_resource

EXECUTE_QUERY = (
    "query ExecuteQuery($schema_name: String!, $namespace_path: String!, $spec: JSON!) "
    "{ execute_query(schema_name: $schema_name, namespace_path: $namespace_path, spec: $spec) }"
)
EXECUTE_COUNT = (
    "query ExecuteCount($schema_name: String!, $namespace_path: String!, $spec: JSON!) "
    "{ execute_count(schema_name: $schema_name, namespace_path: $namespace_path, spec: $spec) }"
)


def transport(body, *, request_id=None):
    value = MagicMock(spec=HTTPTransport)
    value.request.return_value = HTTPResult(body, None, request_id)
    value.redact.side_effect = lambda message: message
    return value


def test_query_posts_complete_spec_and_hydrates_nested_state():
    from recap.adapter.graphql import GraphQLAdapter
    from recap.dsl.query import ParameterFilter, PropertyFilter

    parent_id = uuid4()
    resource = full_resource()
    spec = QuerySpec(
        filters={"name": "sample"}, preloads=("children", "properties"), limit=25,
        offset=5, property_filters=[PropertyFilter(name="temperature", value=20)],
        parent_resource_id=parent_id,
        parameter_filters=[ParameterFilter(name="exposure", step="Acquire", value=2.5)],
        include_archived=True, load_mode="none", on_unloaded="raise",
    )
    value = transport({"data": {"execute_query": {
        "schema_name": "ResourceSchema", "items": [serialize_model(resource)]
    }}})

    [hydrated] = GraphQLAdapter("http://recap.test/graphql", _transport=value).query(
        ResourceSchema, spec, namespace_path="beamline/amx"
    )

    value.request.assert_called_once()
    assert value.request.call_args.args[:2] == ("POST", "http://recap.test/graphql")
    assert value.request.call_args.kwargs["json"] == {
        "query": EXECUTE_QUERY,
        "variables": {
            "schema_name": "ResourceSchema",
            "namespace_path": "beamline/amx",
            "spec": {
                "filters": {"name": "sample"},
                "predicates": [],
                "orderings": [],
                "preloads": ["children", "properties"],
                "limit": 25,
                "offset": 5,
                "property_filters": [{
                    "name": "temperature", "group": None, "op": "eq",
                    "value": 20, "upper": None, "value_type": None,
                }],
                "parent_resource_id": str(parent_id),
                "parameter_filters": [{
                    "name": "exposure", "group": None, "step": "Acquire",
                    "op": "eq", "value": 2.5, "upper": None, "value_type": None,
                }],
                "include_archived": True,
                "local_metadata_filters": {},
                "effective_metadata_filters": {},
                "load_mode": "none",
                "on_unloaded": "raise",
            },
        },
    }
    assert hydrated.children["child"].name == "child"
    assert hydrated._loaded_relations == {"children": True, "properties": True}
    assert hydrated._on_unloaded == "raise"
    assert hydrated.children["child"]._loaded_relations == {
        "children": False, "properties": False,
    }
    assert hydrated.children["child"]._on_unloaded == "silent"


def test_count_uses_transport_and_returns_count():
    from recap.adapter.graphql import GraphQLAdapter
    from recap.dsl.query import PropertyFilter, QuerySpec

    spec = QuerySpec(
        filters={"name": "sample"},
        property_filters=[PropertyFilter(name="temperature", op="gte", value=20)],
    )
    value = transport({"data": {"execute_count": 42}})
    assert GraphQLAdapter("http://recap.test/graphql", _transport=value).count(
        ResourceSchema, spec, namespace_path="beamline/amx"
    ) == 42
    request = value.request.call_args
    assert request.args[:2] == ("POST", "http://recap.test/graphql")
    assert request.kwargs["json"]["query"] == EXECUTE_COUNT
    assert request.kwargs["json"]["variables"] == {
        "schema_name": "ResourceSchema", "namespace_path": "beamline/amx",
        "spec": {
            "filters": {"name": "sample"}, "predicates": [], "orderings": [],
            "preloads": [], "limit": None, "offset": None,
            "property_filters": [{
                "name": "temperature", "group": None, "op": "gte",
                "value": 20, "upper": None, "value_type": None,
            }],
            "parent_resource_id": None, "parameter_filters": [],
            "include_archived": False, "local_metadata_filters": {},
            "effective_metadata_filters": {}, "load_mode": None,
            "on_unloaded": None,
        },
    }


@pytest.mark.parametrize(
    ("code", "error_type"),
    [
        ("authentication_required", RecapAuthenticationError),
        ("permission_denied", RecapPermissionDeniedError),
        ("not_found", RecapNotFoundError),
        ("validation_error", RecapValidationError),
        ("conflict", RecapConflictError),
        ("service_unavailable", RecapServiceUnavailableError),
        ("internal_error", RecapInternalError),
        ("request_error", RecapRequestError),
    ],
)
@pytest.mark.parametrize("method", ("query", "count"))
def test_http_200_graphql_errors_use_public_categories(code, error_type, method):
    from recap.adapter.graphql import GraphQLAdapter
    from recap.dsl.query import QuerySpec

    value = transport({"data": None, "errors": [{
        "message": "Safe message", "extensions": {"code": code, "request_id": "request-5"}
    }]})
    with pytest.raises(error_type) as caught:
        getattr(GraphQLAdapter("http://recap.test/graphql", _transport=value), method)(
            ResourceSchema, QuerySpec(), namespace_path="beamline/amx"
        )
    assert caught.value.request_id == "request-5"
    assert caught.value.code == code
    assert caught.value.url == "http://recap.test/graphql"
    assert caught.value.status_code is None
    assert str(caught.value).startswith("Safe message")
    assert "secret" not in str(caught.value)
    assert "raw internal" not in str(caught.value)


def test_graphql_error_messages_are_redacted_by_transport():
    from recap.adapter.graphql import GraphQLAdapter

    value = transport({"data": None, "errors": [{
        "message": "wire-secret", "extensions": {
            "code": "internal_error", "request_id": "request-5",
        },
    }]})
    value.redact.side_effect = lambda message: message.replace("wire-secret", "**********")

    with pytest.raises(RecapInternalError, match=r"\*{10}") as caught:
        GraphQLAdapter("http://recap.test/graphql", _transport=value).query(
            ResourceSchema, QuerySpec(), namespace_path="beamline/amx"
        )

    assert "wire-secret" not in str(caught.value)
    value.redact.assert_called_once_with("wire-secret")


@pytest.mark.parametrize("errors", [
    None, {"message": "bad"}, "bad", [{"message": "bad"}],
    [{"message": "bad", "extensions": {"code": "future", "request_id": "r"}}],
    [{"message": "bad", "extensions": {"code": "validation_error"}}],
])
def test_malformed_graphql_errors_raise_protocol_error(errors):
    from recap.adapter.graphql import GraphQLAdapter
    from recap.dsl.query import QuerySpec

    value = transport({"data": None, "errors": errors})
    with pytest.raises(RecapProtocolError, match="Malformed GraphQL error response"):
        GraphQLAdapter("http://recap.test/graphql", _transport=value).query(
            ResourceSchema, QuerySpec(), namespace_path="beamline/amx"
        )


@pytest.mark.parametrize("body", [
    {}, {"data": None}, {"data": {}}, {"data": {"execute_count": "bad"}},
    {"data": {"execute_query": {"items": []}}},
])
@pytest.mark.parametrize("method", ("query", "count"))
def test_malformed_graphql_success_response_raises_protocol_error(body, method):
    from recap.adapter.graphql import GraphQLAdapter
    from recap.dsl.query import QuerySpec

    with pytest.raises(RecapProtocolError, match="Malformed GraphQL response"):
        getattr(GraphQLAdapter("http://recap.test/graphql", _transport=transport(body)), method)(
            ResourceSchema, QuerySpec(), namespace_path="beamline/amx"
        )


def test_permissions_are_hydrated():
    from recap.adapter.graphql import GraphQLAdapter
    from recap.client.permissions import ActorPermissions

    value = transport({"data": {"permissions": {
        "identities": [{"provider": "api-key", "subject": "single-user"}],
        "snapshot_generation": "generation-7", "effective_scopes": ["resource:read"],
        "matched_namespace_paths": ["beamline/amx"], "groups": ["amx-users"],
        "roles": ["reader"],
    }}})
    permissions = GraphQLAdapter("http://recap.test/graphql", _transport=value).permissions(
        "beamline/amx"
    )
    assert isinstance(permissions, ActorPermissions)
    assert permissions.identities[0].provider == "api-key"
    assert permissions.identities[0].subject == "single-user"
    assert permissions.snapshot_generation == "generation-7"
    assert permissions.effective_scopes == frozenset({"resource:read"})
    assert permissions.matched_namespace_paths == ("beamline/amx",)
    assert permissions.groups == ("amx-users",)
    assert permissions.roles == ("reader",)
    assert value.request.call_args.kwargs["json"] == {
        "query": (
            "query Permissions($namespace_path: String!) "
            "{ permissions(namespace_path: $namespace_path) "
            "{ identities { provider subject } snapshot_generation effective_scopes "
            "matched_namespace_paths groups roles } }"
        ),
        "variables": {"namespace_path": "beamline/amx"},
    }


@pytest.mark.parametrize("method", ("query", "permissions"))
def test_malformed_graphql_method_response_raises_protocol_error(method):
    from recap.adapter.graphql import GraphQLAdapter

    value = transport({"data": {}})
    adapter = GraphQLAdapter("http://recap.test/graphql", _transport=value)
    with pytest.raises(RecapProtocolError, match="Malformed GraphQL response"):
        if method == "query":
            adapter.query(ResourceSchema, QuerySpec(), namespace_path="beamline/amx")
        else:
            adapter.permissions("beamline/amx")


def test_injected_falsey_transport_is_preserved():
    from recap.adapter.graphql import GraphQLAdapter

    class FalseyTransport:
        def __bool__(self):
            return False
        def close(self):
            pass

    value = FalseyTransport()
    assert GraphQLAdapter("http://recap.test/graphql", _transport=value)._transport is value


@pytest.mark.parametrize(
    ("field", "value"),
    [("predicates", (ProcessRun.name == "sample",))],
)
@pytest.mark.parametrize("method", ("query", "count"))
def test_legacy_query_features_are_rejected_before_transport(field, value, method):
    from recap.adapter.graphql import GraphQLAdapter

    value_transport = MagicMock(spec=HTTPTransport)
    with pytest.raises(TypeError, match="Field"):
        getattr(GraphQLAdapter("http://recap.test/graphql", _transport=value_transport), method)(
            ResourceSchema, QuerySpec(**{field: value}), namespace_path="beamline/amx"
        )
    value_transport.request.assert_not_called()


def test_default_transport_receives_timeout():
    from unittest.mock import patch

    from recap.adapter.graphql import GraphQLAdapter

    with patch("recap.adapter.graphql.HTTPTransport") as transport_type:
        adapter = GraphQLAdapter("http://recap.test/graphql", "secret", timeout=12.5)
    transport_type.assert_called_once_with("secret", timeout=12.5)
    adapter.close()
