"""End-to-end integration: write via LocalBackend, read via GraphQL server."""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock


def test_write_local_read_graphql(tmp_path):
    """Write a campaign via LocalBackend directly; read it back via GraphQL."""
    from recap.client import RecapClient
    from recap.server.app import create_app

    db_path = tmp_path / "recap.db"

    # Write directly via local client
    local_client = RecapClient.from_sqlite(str(db_path))
    campaign = local_client.create_campaign(name="Test Campaign", proposal="P-001")
    local_client.close()

    # Read via GraphQL server
    app = create_app(db_path)
    test_client = TestClient(app)

    resp = test_client.post("/graphql", json={"query": "{ campaigns { id name proposal } }"})
    assert resp.status_code == 200
    data = resp.json()["data"]["campaigns"]
    assert len(data) == 1
    assert data[0]["name"] == "Test Campaign"
    assert data[0]["proposal"] == "P-001"


def test_graphql_resources_after_write(tmp_path):
    """Write resources via LocalBackend; verify they appear in GraphQL."""
    from recap.client import RecapClient
    from recap.server.app import create_app

    db_path = tmp_path / "recap.db"
    local_client = RecapClient.from_sqlite(str(db_path))

    with local_client.build_resource_template(name="Sample", type_names=["sample"]) as tmpl:
        pass

    with local_client.build_resource(name="S-001", template_name="Sample") as res:
        pass

    local_client.close()

    app = create_app(db_path)
    test_client = TestClient(app)
    resp = test_client.post("/graphql", json={"query": "{ resources { id name } resourcesCount }"})
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["resourcesCount"] == 1
    assert body["resources"][0]["name"] == "S-001"


def test_graphql_limit_enforced(tmp_path):
    """Server enforces max limit of 10000."""
    from recap.server.app import create_app
    app = create_app(tmp_path / "recap.db")
    test_client = TestClient(app)
    resp = test_client.post("/graphql", json={"query": "{ resources(limit: 99999) { id } }"})
    assert resp.status_code == 200
    body = resp.json()
    assert "errors" in body
