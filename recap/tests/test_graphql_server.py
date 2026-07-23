from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from recap.client import RecapClient


def make_test_app(tmp_path):
    from recap.server.app import create_app

    return create_app(tmp_path / "test.db")


EXECUTE_QUERY = """
query ExecuteQuery($schema_name: String!, $spec: JSON!) {
  execute_query(schema_name: $schema_name, spec: $spec)
}
"""

EXECUTE_COUNT = """
query ExecuteCount($schema_name: String!, $spec: JSON!) {
  execute_count(schema_name: $schema_name, spec: $spec)
}
"""


def seed_resource_tree(db_path):
    client = RecapClient.from_sqlite(db_path)
    with client.build_resource_template(
        name="Parent", type_names=["container"]
    ) as builder:
        builder.close_child()
    with client.build_resource_template(name="Child", type_names=["sample"]) as builder:
        builder.close_child()
    with client.build_resource("root", "Parent") as builder:
        builder.add_child("nested", "Child")
    client.close()


def test_db_path_endpoint(tmp_path):
    app = make_test_app(tmp_path)
    client = TestClient(app)
    resp = client.get("/db_path")
    assert resp.status_code == 200
    data = resp.json()
    assert "db_path" in data
    assert "test.db" in data["db_path"]


def test_graphql_endpoint_responds(tmp_path):
    app = make_test_app(tmp_path)
    client = TestClient(app)
    resp = client.post("/graphql", json={"query": "{ campaigns { id name } }"})
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    assert "campaigns" in body["data"]


def test_graphql_resources_empty(tmp_path):
    app = make_test_app(tmp_path)
    client = TestClient(app)
    resp = client.post("/graphql", json={"query": "{ resources { id name } }"})
    assert resp.status_code == 200
    assert resp.json()["data"]["resources"] == []


def test_graphql_count_fields(tmp_path):
    app = make_test_app(tmp_path)
    client = TestClient(app)
    resp = client.post(
        "/graphql",
        json={
            "query": "{ resources_count campaigns_count resource_templates_count process_templates_count }"
        },
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["resources_count"] == 0
    assert data["campaigns_count"] == 0
    assert data["resource_templates_count"] == 0
    assert data["process_templates_count"] == 0


def test_execute_query_posts_full_spec_and_returns_nested_transport_payload(tmp_path):
    db_path = tmp_path / "test.db"
    seed_resource_tree(db_path)
    client = TestClient(make_test_app(tmp_path))

    response = client.post(
        "/graphql",
        json={
            "query": EXECUTE_QUERY,
            "variables": {
                "schema_name": "ResourceSchema",
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


def test_execute_query_supports_ref_schema(tmp_path):
    db_path = tmp_path / "test.db"
    seed_resource_tree(db_path)
    client = TestClient(make_test_app(tmp_path))

    response = client.post(
        "/graphql",
        json={
            "query": EXECUTE_QUERY,
            "variables": {
                "schema_name": "ResourceRef",
                "spec": {"filters": {"name": "nested"}},
            },
        },
    )

    assert response.status_code == 200
    result = response.json()["data"]["execute_query"]
    assert result["schema_name"] == "ResourceRef"
    assert result["items"][0]["name"] == "nested"
    assert "__recap__" not in result["items"][0]


def test_execute_count_supports_filters(tmp_path):
    db_path = tmp_path / "test.db"
    seed_resource_tree(db_path)
    client = TestClient(make_test_app(tmp_path))

    response = client.post(
        "/graphql",
        json={
            "query": EXECUTE_COUNT,
            "variables": {
                "schema_name": "ResourceSchema",
                "spec": {"filters": {"name": "root"}},
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["execute_count"] == 1


def test_execute_query_rejects_unknown_schema(tmp_path):
    client = TestClient(make_test_app(tmp_path))

    response = client.post(
        "/graphql",
        json={
            "query": EXECUTE_QUERY,
            "variables": {"schema_name": "attacker-controlled", "spec": {}},
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
def test_execute_rejects_malformed_query_spec(tmp_path, query):
    client = TestClient(make_test_app(tmp_path))

    response = client.post(
        "/graphql",
        json={
            "query": query,
            "variables": {
                "schema_name": "ResourceSchema",
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
        spec={},
    )

    query_spec = backend.query.call_args.args[1]
    assert query_spec.limit is None
