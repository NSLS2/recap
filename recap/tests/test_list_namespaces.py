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


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        pytest.param("", ["beamline", "staff"], id="root"),
        pytest.param("beamline", ["amx", "fmx"], id="direct-children"),
        pytest.param("beamline/amx", [], id="leaf"),
    ],
)
def test_list_namespaces_returns_direct_children(client_with_namespaces, scope, expected):
    client = client_with_namespaces if not scope else client_with_namespaces[scope]
    assert sorted(client.list_namespaces()) == expected


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

    assert sorted(client.backend.namespaces.list_child_namespace_paths("")) == [
        "beamline",
        "staff",
    ]
    assert sorted(client.backend.namespaces.list_child_namespace_paths("beamline")) == [
        "beamline/amx",
        "beamline/fmx",
    ]
    assert client.backend.namespaces.list_child_namespace_paths("beamline/amx") == []
    assert client.backend.namespaces.list_child_namespace_paths("missing") == []


def test_list_namespaces_remote_calls_scoped_rest_children_endpoint():
    from recap.adapter.rest import RESTResult

    client = RecapClient.from_url("http://recap.test", api_key="secret")
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path))
        return RESTResult(entity=["amx", "fmx"], etag=None, request_id=None)

    client.backend.namespaces._request = fake_request

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

    client.backend.namespaces._request = fake_request

    result = client.list_namespaces()

    assert calls == [("GET", "/api/v1/namespaces/children")]
    assert sorted(result) == ["beamline", "staff"]
    client.close()
