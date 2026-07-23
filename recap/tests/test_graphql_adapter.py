from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from recap.adapter.transport import serialize_model
from recap.dsl.query import ParameterFilter, PropertyFilter, QuerySpec
from recap.schemas.resource import ResourceSchema
from recap.tests.transport_factories import full_resource

EXECUTE_QUERY = (
    "query ExecuteQuery($schema_name: String!, $spec: JSON!) "
    "{ execute_query(schema_name: $schema_name, spec: $spec) }"
)
EXECUTE_COUNT = (
    "query ExecuteCount($schema_name: String!, $spec: JSON!) "
    "{ execute_count(schema_name: $schema_name, spec: $spec) }"
)


def response_with(body):
    response = MagicMock()
    response.json.return_value = body
    return response


def test_graphql_adapter_query_posts_complete_spec_and_hydrates_nested_state():
    from recap.adapter.graphql import GraphQLAdapter

    campaign_id = uuid4()
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
        campaign_id=campaign_id,
        load_mode="none",
        on_unloaded="raise",
    )

    with (
        patch("httpx2.Client.post", return_value=response) as post,
        GraphQLAdapter("http://localhost:9999/graphql") as adapter,
    ):
        [hydrated] = adapter.query(ResourceSchema, spec)

    post.assert_called_once_with(
        "http://localhost:9999/graphql",
        json={
            "query": EXECUTE_QUERY,
            "variables": {
                "schema_name": "ResourceSchema",
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
                    "campaign_id": str(campaign_id),
                    "load_mode": "none",
                    "on_unloaded": "raise",
                },
            },
        },
    )
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
        GraphQLAdapter("http://localhost:9999/graphql") as adapter,
    ):
        count = adapter.count(ResourceSchema, spec)

    assert count == 42
    payload = post.call_args.kwargs["json"]
    assert payload["query"] == EXECUTE_COUNT
    assert payload["variables"]["schema_name"] == "ResourceSchema"
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
        getattr(adapter, method)(ResourceSchema, QuerySpec())

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
        adapter.query(ResourceSchema, QuerySpec())

    assert str(exc_info.value) == "GraphQL request failed: malformed error response"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("predicates", (lambda item: True,), "predicates"),
        ("orderings", (lambda item: item.name,), "orderings"),
    ],
)
def test_graphql_adapter_rejects_non_transportable_query_features(
    field, value, message
):
    from recap.adapter.graphql import GraphQLAdapter

    with (
        GraphQLAdapter("http://localhost:9999/graphql") as adapter,
        pytest.raises(NotImplementedError, match=message),
    ):
        adapter.query(ResourceSchema, QuerySpec(**{field: value}))
