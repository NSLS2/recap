from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from recap.adapter.transport import serialize_model
from recap.db.process import ProcessRun
from recap.dsl.query import Field, ParameterFilter, PropertyFilter, QuerySpec
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


def response_with(body):
    response = MagicMock()
    response.json.return_value = body
    return response


def test_graphql_adapter_query_posts_complete_spec_and_hydrates_nested_state():
    from recap.adapter.graphql import GraphQLAdapter

    parent_id = uuid4()
    resource = full_resource()
    response = response_with(
        {
            "data": {
                "execute_query": {
                    "schema_name": "ResourceSchema",
                    "items": [serialize_model(resource)],
                }
            }
        }
    )
    spec = QuerySpec(
        filters={"name": "sample"},
        preloads=("children", "properties"),
        limit=25,
        offset=5,
        property_filters=[PropertyFilter(name="temperature", value=20)],
        parent_resource_id=parent_id,
        parameter_filters=[ParameterFilter(name="exposure", step="Acquire", value=2.5)],
        include_archived=True,
        load_mode="none",
        on_unloaded="raise",
    )

    with (
        patch("httpx2.Client.post", return_value=response) as post,
        GraphQLAdapter(
            "http://localhost:9999/graphql", api_key="client-secret"
        ) as adapter,
    ):
        [hydrated] = adapter.query(ResourceSchema, spec, namespace_path="beamline/amx")

    post.assert_called_once_with(
        "http://localhost:9999/graphql",
        json={
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
                    "property_filters": [
                        {
                            "name": "temperature",
                            "group": None,
                            "op": "eq",
                            "value": 20,
                            "upper": None,
                            "value_type": None,
                        }
                    ],
                    "parent_resource_id": str(parent_id),
                    "parameter_filters": [
                        {
                            "name": "exposure",
                            "group": None,
                            "step": "Acquire",
                            "op": "eq",
                            "value": 2.5,
                            "upper": None,
                            "value_type": None,
                        }
                    ],
                    "include_archived": True,
                    "local_metadata_filters": {},
                    "effective_metadata_filters": {},
                    "load_mode": "none",
                    "on_unloaded": "raise",
                },
            },
        },
        headers={"Authorization": "Apikey client-secret"},
    )
    assert "namespace_path" not in post.call_args.kwargs["json"]["variables"]["spec"]
    assert hydrated.children["child"].name == "child"
    assert hydrated._loaded_relations == {"children": True, "properties": True}
    assert hydrated._on_unloaded == "raise"
    assert hydrated.children["child"]._loaded_relations == {
        "children": False,
        "properties": False,
    }
    assert hydrated.children["child"]._on_unloaded == "silent"


def test_graphql_adapter_count_posts_filters_in_complete_spec():
    from recap.adapter.graphql import GraphQLAdapter

    response = response_with({"data": {"execute_count": 42}})
    spec = QuerySpec(
        filters={"name": "sample"},
        property_filters=[PropertyFilter(name="temperature", op="gte", value=20)],
    )

    with (
        patch("httpx2.Client.post", return_value=response) as post,
        GraphQLAdapter(
            "http://localhost:9999/graphql", api_key="client-secret"
        ) as adapter,
    ):
        count = adapter.count(ResourceSchema, spec, namespace_path="beamline/amx")

    assert count == 42
    payload = post.call_args.kwargs["json"]
    assert post.call_args.kwargs["headers"] == {"Authorization": "Apikey client-secret"}
    assert payload["query"] == EXECUTE_COUNT
    assert payload["variables"]["schema_name"] == "ResourceSchema"
    assert payload["variables"]["namespace_path"] == "beamline/amx"
    assert "namespace_path" not in payload["variables"]["spec"]
    assert payload["variables"]["spec"]["filters"] == {"name": "sample"}
    assert payload["variables"]["spec"]["property_filters"] == [
        {
            "name": "temperature",
            "group": None,
            "op": "gte",
            "value": 20,
            "upper": None,
            "value_type": None,
        }
    ]


@pytest.mark.parametrize("method", ("query", "count"))
def test_graphql_adapter_rejects_http_200_graphql_errors(method):
    from recap.adapter.graphql import GraphQLAdapter

    response = response_with(
        {
            "data": None,
            "errors": [
                {
                    "message": "Invalid query specification",
                    "locations": [{"line": 1, "column": 1}],
                }
            ],
        }
    )

    with (
        patch("httpx2.Client.post", return_value=response),
        GraphQLAdapter("http://localhost:9999/graphql") as adapter,
        pytest.raises(RuntimeError) as exc_info,
    ):
        getattr(adapter, method)(
            ResourceSchema, QuerySpec(), namespace_path="beamline/amx"
        )

    assert str(exc_info.value) == "GraphQL request failed: Invalid query specification"
    assert "locations" not in str(exc_info.value)


@pytest.mark.parametrize(
    "errors",
    (
        {"message": "Invalid query specification"},
        "Invalid query specification",
        ["Invalid query specification"],
    ),
)
def test_graphql_adapter_rejects_malformed_graphql_errors(errors):
    from recap.adapter.graphql import GraphQLAdapter

    response = response_with({"data": None, "errors": errors})

    with (
        patch("httpx2.Client.post", return_value=response),
        GraphQLAdapter("http://localhost:9999/graphql") as adapter,
        pytest.raises(RuntimeError) as exc_info,
    ):
        adapter.query(ResourceSchema, QuerySpec(), namespace_path="beamline/amx")

    assert str(exc_info.value) == "GraphQL request failed: malformed error response"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("predicates", (ProcessRun.name == "sample",)),
        (
            "predicates",
            (Field("name") == "sample", ProcessRun.name == "sample"),
        ),
        ("orderings", (ProcessRun.name,)),
        ("orderings", (Field("name").asc(), ProcessRun.name.desc())),
    ],
)
@pytest.mark.parametrize("method", ("query", "count"))
def test_graphql_adapter_rejects_legacy_query_features_before_http(
    field, value, method
):
    from recap.adapter.graphql import GraphQLAdapter

    with (
        patch("httpx2.Client.post") as post,
        GraphQLAdapter("http://localhost:9999/graphql") as adapter,
        pytest.raises(TypeError, match="Field"),
    ):
        getattr(adapter, method)(
            ResourceSchema,
            QuerySpec(**{field: value}),
            namespace_path="beamline/amx",
        )

    post.assert_not_called()


def test_graphql_adapter_permissions_returns_typed_current_actor_permissions():
    from recap.adapter.graphql import GraphQLAdapter
    from recap.authentication.models import ProviderIdentity
    from recap.authorization.scopes import Scope
    from recap.client.permissions import ActorPermissions

    response = response_with(
        {
            "data": {
                "permissions": {
                    "identities": [{"provider": "api-key", "subject": "single-user"}],
                    "snapshot_generation": "generation-7",
                    "effective_scopes": ["resource:read"],
                    "matched_namespace_paths": ["beamline/amx"],
                    "groups": ["amx-users"],
                    "roles": ["reader"],
                }
            }
        }
    )

    with (
        patch("httpx2.Client.post", return_value=response) as post,
        GraphQLAdapter(
            "http://localhost:9999/graphql", api_key="client-secret"
        ) as adapter,
    ):
        permissions = adapter.permissions("beamline/amx")

    assert isinstance(permissions, ActorPermissions)
    assert permissions.identities == (
        ProviderIdentity(provider="api-key", subject="single-user"),
    )
    assert permissions.effective_scopes == frozenset({Scope.RESOURCE_READ})
    assert permissions.groups == ("amx-users",)
    assert permissions.roles == ("reader",)
    assert post.call_args.kwargs["json"]["variables"] == {
        "namespace_path": "beamline/amx"
    }
    assert post.call_args.kwargs["headers"] == {"Authorization": "Apikey client-secret"}


def test_graphql_adapter_redacts_api_key_from_repr_and_graphql_errors():
    from recap.adapter.graphql import GraphQLAdapter

    api_key = "never-print-client-secret"
    response = response_with(
        {"data": None, "errors": [{"message": f"rejected {api_key}"}]}
    )
    adapter = GraphQLAdapter("http://localhost:9999/graphql", api_key=api_key)

    assert api_key not in repr(adapter)
    with (
        patch("httpx2.Client.post", return_value=response),
        pytest.raises(RuntimeError) as exc_info,
    ):
        adapter.query(ResourceSchema, QuerySpec(), namespace_path="beamline/amx")

    assert api_key not in str(exc_info.value)
    assert "**********" in str(exc_info.value)
    adapter.close()


def test_graphql_adapter_redacts_api_key_from_transport_errors():
    from recap.adapter.graphql import GraphQLAdapter

    api_key = "never-print-transport-secret"
    response = response_with({})
    response.raise_for_status.side_effect = RuntimeError(f"rejected {api_key}")

    with (
        patch("httpx2.Client.post", return_value=response),
        GraphQLAdapter("http://localhost:9999/graphql", api_key=api_key) as adapter,
        pytest.raises(RuntimeError) as exc_info,
    ):
        adapter.query(ResourceSchema, QuerySpec(), namespace_path="beamline/amx")

    assert api_key not in str(exc_info.value)
    assert "**********" in str(exc_info.value)
    assert exc_info.value.__cause__ is None
