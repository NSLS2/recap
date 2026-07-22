from pathlib import Path
import pytest
from fastapi.testclient import TestClient


def make_test_app(tmp_path):
    from recap.server.app import create_app
    return create_app(tmp_path / "test.db")


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
    resp = client.post("/graphql", json={"query": "{ resourcesCount campaignsCount resourceTemplatesCount processTemplatesCount }"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["resourcesCount"] == 0
    assert data["campaignsCount"] == 0
    assert data["resourceTemplatesCount"] == 0
    assert data["processTemplatesCount"] == 0
