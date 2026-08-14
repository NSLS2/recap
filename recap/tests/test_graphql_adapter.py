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
    assert hydrated.children["child"].name == "child"
    assert hydrated._loaded_relations == {"children": True, "properties": True}
    assert hydrated._on_unloaded == "raise"


def test_count_uses_transport_and_returns_count():
    from recap.adapter.graphql import GraphQLAdapter
    from recap.dsl.query import QuerySpec

    value = transport({"data": {"execute_count": 42}})
    assert GraphQLAdapter("http://recap.test/graphql", _transport=value).count(
        ResourceSchema, QuerySpec(), namespace_path="beamline/amx"
    ) == 42
    request = value.request.call_args
    assert request.args[:2] == ("POST", "http://recap.test/graphql")
    assert request.kwargs["json"]["query"] == EXECUTE_COUNT


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
    assert str(caught.value).startswith("Safe message")


@pytest.mark.parametrize("errors", [
    {"message": "bad"}, "bad", [{"message": "bad"}],
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
def test_malformed_graphql_success_response_raises_protocol_error(body):
    from recap.adapter.graphql import GraphQLAdapter
    from recap.dsl.query import QuerySpec

    with pytest.raises(RecapProtocolError, match="Malformed GraphQL response"):
        GraphQLAdapter("http://recap.test/graphql", _transport=transport(body)).count(
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
    assert permissions.groups == ("amx-users",)


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
