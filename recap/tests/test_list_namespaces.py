import pytest
from recap.client import RecapClient


@pytest.fixture
def client_with_namespaces(tmp_path):
    """Client with namespace tree: beamline/amx, beamline/fmx, staff"""
    with RecapClient.from_sqlite(tmp_path / "recap.db") as client:
        client.create_namespace("beamline")
        client.create_namespace("beamline/amx")
        client.create_namespace("beamline/fmx")
        client.create_namespace("staff")
        yield client


def test_list_namespaces_root_returns_top_level(client_with_namespaces):
    result = client_with_namespaces.list_namespaces()
    assert sorted(result) == ["beamline", "staff"]


def test_list_namespaces_scoped_returns_direct_children(client_with_namespaces):
    result = client_with_namespaces["beamline"].list_namespaces()
    assert sorted(result) == ["amx", "fmx"]


def test_list_namespaces_explicit_hierarchy(tmp_path):
    with RecapClient.from_sqlite(tmp_path / "recap.db") as client:
        client.create_namespace("beamline")
        client.create_namespace("beamline/amx")
        client.create_namespace("beamline/fmx")

        assert client.list_namespaces() == ["beamline"]
        assert client["beamline"].list_namespaces() == ["amx", "fmx"]


def test_list_namespaces_leaf_returns_empty(client_with_namespaces):
    result = client_with_namespaces["beamline/amx"].list_namespaces()
    assert result == []


def test_list_namespaces_nonexistent_returns_empty(tmp_path):
    with RecapClient.from_sqlite(tmp_path / "recap.db") as client:
        result = client["nonexistent"].list_namespaces()
        assert result == []


def test_list_namespaces_only_direct_children_not_grandchildren(client_with_namespaces):
    """beamline/amx/detector exists but should not appear in beamline children"""
    client_with_namespaces.create_namespace("beamline/amx/detector")
    result = client_with_namespaces["beamline"].list_namespaces()
    assert sorted(result) == ["amx", "fmx"]


def test_local_backend_lists_full_direct_child_paths(client_with_namespaces):
    client = client_with_namespaces

    assert sorted(client.backend.list_child_namespace_paths("")) == [
        "beamline",
        "staff",
    ]
    assert sorted(client.backend.list_child_namespace_paths("beamline")) == [
        "beamline/amx",
        "beamline/fmx",
    ]
    assert client.backend.list_child_namespace_paths("beamline/amx") == []
    assert client.backend.list_child_namespace_paths("missing") == []


def test_list_namespaces_remote_calls_correct_url():
    from recap.adapter.rest import RESTResult

    client = RecapClient.from_url("http://recap.test", api_key="secret")
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path))
        return RESTResult(entity=["amx", "fmx"], etag=None, request_id=None)

    client.backend._request = fake_request

    result = client["beamline"].list_namespaces()

    assert calls == [("GET", "/api/v1/namespaces/children/beamline")]
    assert sorted(result) == ["amx", "fmx"]
    client.close()


def test_list_namespaces_remote_root_calls_correct_url():
    from recap.adapter.rest import RESTResult

    client = RecapClient.from_url("http://recap.test", api_key="secret")
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path))
        return RESTResult(entity=["beamline", "staff"], etag=None, request_id=None)

    client.backend._request = fake_request

    result = client.list_namespaces()

    assert calls == [("GET", "/api/v1/namespaces/children")]
    assert sorted(result) == ["beamline", "staff"]
    client.close()
