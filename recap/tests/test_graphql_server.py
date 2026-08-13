from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

def make_test_app(tmp_path):
    from recap.server.app import create_app

    return create_app(tmp_path / "test.db")


EXECUTE_QUERY = """
query ExecuteQuery($schema_name: String!, $namespace_path: String!, $spec: JSON!) {
  execute_query(schema_name: $schema_name, namespace_path: $namespace_path, spec: $spec)
}
"""

EXECUTE_COUNT = """
query ExecuteCount($schema_name: String!, $namespace_path: String!, $spec: JSON!) {
  execute_count(schema_name: $schema_name, namespace_path: $namespace_path, spec: $spec)
}
"""


def test_db_path_endpoint_is_removed(tmp_path):
    app = make_test_app(tmp_path)
    client = TestClient(app)
    resp = client.get("/db_path")
    assert resp.status_code == 404


def test_graphql_endpoint_responds(integration_database_path, graphql_namespace_path):
    app = make_test_app(integration_database_path.parent)
    client = TestClient(app)
    resp = client.post(
        "/graphql",
        json={
            "query": f'{{ namespaces(namespace_path: "{graphql_namespace_path}") {{ id path }} }}'
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    assert "namespaces" in body["data"]


def test_graphql_namespaces_include_mutable_namespaces(tmp_path):
    from recap.server.app import create_app

    api_key = "secret"
    client = TestClient(create_app(tmp_path / "mutable.db", api_key=api_key))
    headers = {"Authorization": f"Apikey {api_key}"}

    response = client.put(
        "/api/v1/namespaces/test",
        headers={**headers, "Idempotency-Key": "test-namespace"},
        json={"metadata": {}},
    )
    assert response.status_code == 201

    response = client.post(
        "/graphql",
        headers=headers,
        json={"query": '{ namespaces(namespace_path: "test") { path } }'},
    )

    assert response.json()["data"]["namespaces"] == [{"path": "test"}]


def test_graphql_resources_empty(integration_database_path, graphql_namespace_path):
    app = make_test_app(integration_database_path.parent)
    client = TestClient(app)
    resp = client.post(
        "/graphql",
        json={
            "query": f'{{ resources(namespace_path: "{graphql_namespace_path}") {{ id name }} }}'
        },
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["resources"] == []


def test_integration_seed_contains_disjoint_graphql_scopes(
    integration_database_path,
    graphql_namespace_path,
    graphql_resource_tree_path,
):
    client = TestClient(make_test_app(integration_database_path.parent))

    empty_response = client.post(
        "/graphql",
        json={
            "query": f'{{ resources(namespace_path: "{graphql_namespace_path}") {{ name }} }}'
        },
    )
    tree_response = client.post(
        "/graphql",
        json={
            "query": f'{{ resources(namespace_path: "{graphql_resource_tree_path}") {{ name }} }}'
        },
    )

    assert empty_response.json()["data"]["resources"] == []
    assert [item["name"] for item in tree_response.json()["data"]["resources"]] == [
        "root",
        "nested",
    ]


def test_graphql_count_fields(integration_database_path, graphql_namespace_path):
    app = make_test_app(integration_database_path.parent)
    client = TestClient(app)
    resp = client.post(
        "/graphql",
        json={
            "query": "{ "
            f'resources_count(namespace_path: "{graphql_namespace_path}") '
            f'namespaces_count(namespace_path: "{graphql_namespace_path}") '
            f'resource_templates_count(namespace_path: "{graphql_namespace_path}") '
            f'process_templates_count(namespace_path: "{graphql_namespace_path}")'
            " }"
        },
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["resources_count"] == 0
    assert data["namespaces_count"] == 1
    assert data["resource_templates_count"] == 0
    assert data["process_templates_count"] == 0


def test_execute_query_posts_full_spec_and_returns_nested_transport_payload(
    integration_database_path, graphql_resource_tree_path
):
    client = TestClient(make_test_app(integration_database_path.parent))

    response = client.post(
        "/graphql",
        json={
            "query": EXECUTE_QUERY,
            "variables": {
                "schema_name": "ResourceSchema",
                "namespace_path": graphql_resource_tree_path,
                "spec": {
                    "filters": {"name": "root"},
                    "preloads": ["children", "template"],
                    "limit": 1,
                    "offset": 0,
                    "load_mode": "none",
                    "on_unloaded": "raise",
                },
            },
        },
    )

    assert response.status_code == 200
    result = response.json()["data"]["execute_query"]
    assert result["schema_name"] == "ResourceSchema"
    assert result["items"][0]["children"]["nested"]["name"] == "nested"
    assert result["items"][0]["__recap__"] == {
        "loaded_relations": {
            "properties": False,
            "children": True,
        },
        "on_unloaded": "raise",
    }


def test_execute_query_supports_ref_schema(
    integration_database_path, graphql_resource_tree_path
):
    client = TestClient(make_test_app(integration_database_path.parent))

    response = client.post(
        "/graphql",
        json={
            "query": EXECUTE_QUERY,
            "variables": {
                "schema_name": "ResourceRef",
                "namespace_path": graphql_resource_tree_path,
                "spec": {"filters": {"name": "nested"}},
            },
        },
    )

    assert response.status_code == 200
    result = response.json()["data"]["execute_query"]
    assert result["schema_name"] == "ResourceRef"
    assert result["items"][0]["name"] == "nested"
    assert "__recap__" not in result["items"][0]


def test_execute_count_supports_filters(
    integration_database_path, graphql_resource_tree_path
):
    client = TestClient(make_test_app(integration_database_path.parent))

    response = client.post(
        "/graphql",
        json={
            "query": EXECUTE_COUNT,
            "variables": {
                "schema_name": "ResourceSchema",
                "namespace_path": graphql_resource_tree_path,
                "spec": {"filters": {"name": "root"}},
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["execute_count"] == 1


def test_execute_query_rejects_unknown_schema(
    integration_database_path, graphql_namespace_path
):
    client = TestClient(make_test_app(integration_database_path.parent))

    response = client.post(
        "/graphql",
        json={
            "query": EXECUTE_QUERY,
            "variables": {
                "schema_name": "attacker-controlled",
                "namespace_path": graphql_namespace_path,
                "spec": {},
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data"] is None
    assert body["errors"][0]["message"] == "Unknown query schema"
    assert "attacker-controlled" not in str(body)


@pytest.mark.parametrize(
    "query",
    (EXECUTE_QUERY, EXECUTE_COUNT),
    ids=("query", "count"),
)
def test_execute_rejects_malformed_query_spec(
    integration_database_path, graphql_namespace_path, query
):
    client = TestClient(make_test_app(integration_database_path.parent))

    response = client.post(
        "/graphql",
        json={
            "query": query,
            "variables": {
                "schema_name": "ResourceSchema",
                "namespace_path": graphql_namespace_path,
                "spec": {"load_mode": "attacker-controlled"},
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data"] is None
    assert body["errors"][0]["message"] == "Invalid query specification"
    assert "attacker-controlled" not in str(body)


def test_execute_query_preserves_unlimited_spec():
    from recap.server.resolvers import resolve_execute_query

    backend = Mock()
    backend.query.return_value = []

    resolve_execute_query(
        SimpleNamespace(context={"backend": backend}),
        schema_name="ResourceSchema",
        namespace_path="beamline/amx",
        spec={},
    )

    query_spec = backend.query.call_args.args[1]
    assert query_spec.limit is None


def test_execute_query_normalizes_legacy_full_load_mode():
    from recap.server.resolvers import resolve_execute_query

    backend = Mock()
    backend.query.return_value = []

    with pytest.warns(DeprecationWarning, match="load='eager'"):
        result = resolve_execute_query(
            SimpleNamespace(context={"backend": backend}),
            schema_name="ResourceSchema",
            namespace_path="beamline/amx",
            spec={"load_mode": "full"},
        )

    query_spec = backend.query.call_args.args[1]
    assert query_spec.load_mode == "eager"
    assert result == {"schema_name": "ResourceSchema", "items": []}
