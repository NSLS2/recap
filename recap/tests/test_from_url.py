"""Tests for RecapClient.from_url()."""

from unittest.mock import MagicMock, patch

import pytest


def test_from_url_returns_recap_client(tmp_path):
    from recap.client import RecapClient

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"db_path": str(tmp_path / "recap.db")}
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx2.get", return_value=mock_resp):
        client = RecapClient.from_url("http://localhost:8000", api_key="secret")

    assert isinstance(client, RecapClient)
    assert client.backend is not None


def test_from_url_uses_graphql_adapter_for_reads(tmp_path):
    from recap.adapter.graphql import GraphQLAdapter
    from recap.client import RecapClient

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"db_path": str(tmp_path / "recap.db")}
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx2.get", return_value=mock_resp):
        client = RecapClient.from_url("http://localhost:8000", api_key="secret")

    assert isinstance(client._read_backend, GraphQLAdapter)


def test_from_url_uses_local_backend_for_writes(tmp_path):
    from recap.adapter.local import LocalBackend
    from recap.client import RecapClient

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"db_path": str(tmp_path / "recap.db")}
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx2.get", return_value=mock_resp):
        client = RecapClient.from_url("http://localhost:8000", api_key="secret")

    assert isinstance(client.backend, LocalBackend)


def test_from_url_connection_error():
    import httpx2

    from recap.client import RecapClient
    from recap.exceptions import RecapConnectionError

    with (
        patch("httpx2.get", side_effect=httpx2.ConnectError("refused")),
        pytest.raises(RecapConnectionError),
    ):
        RecapClient.from_url("http://localhost:9999", api_key="secret")


def test_from_url_bad_status():
    import httpx2

    from recap.client import RecapClient
    from recap.exceptions import RecapConnectionError

    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = httpx2.HTTPStatusError(
        "404", request=MagicMock(), response=MagicMock(status_code=404)
    )
    with (
        patch("httpx2.get", return_value=mock_resp),
        pytest.raises(RecapConnectionError),
    ):
        RecapClient.from_url("http://localhost:8000", api_key="secret")


def test_from_url_query_maker_uses_read_backend(tmp_path):
    from recap.adapter.graphql import GraphQLAdapter
    from recap.client import RecapClient

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"db_path": str(tmp_path / "recap.db")}
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx2.get", return_value=mock_resp):
        client = RecapClient.from_url("http://localhost:8000", api_key="secret")

    qm = client.query_maker(context=client.create_namespace("test"))
    # QueryDSL stores backend as _backend
    assert isinstance(qm.backend, GraphQLAdapter)


def test_from_url_authenticates_db_path_request(tmp_path):
    from recap.client import RecapClient

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"db_path": str(tmp_path / "recap.db")}

    with patch("httpx2.get", return_value=mock_resp) as get:
        client = RecapClient.from_url("http://localhost:8000", api_key="client-secret")

    get.assert_called_once_with(
        "http://localhost:8000/db_path",
        headers={"Authorization": "Apikey client-secret"},
    )
    assert "client-secret" not in repr(client._read_backend)
    client.close()


def test_from_url_rejects_unscoped_remote_client_before_http():
    from recap.client import RecapClient

    with (
        patch("httpx2.get") as get,
        pytest.raises(ValueError, match="unscoped"),
    ):
        RecapClient.from_url("http://localhost:8000", api_key="secret", unscoped=True)

    get.assert_not_called()


def test_from_url_redacts_api_key_from_connection_error_chain():
    import httpx2

    from recap.client import RecapClient
    from recap.exceptions import RecapConnectionError

    api_key = "never-print-connection-secret"
    with (
        patch(
            "httpx2.get",
            side_effect=httpx2.ConnectError(f"failed with {api_key}"),
        ),
        pytest.raises(RecapConnectionError) as exc_info,
    ):
        RecapClient.from_url("http://localhost:9999", api_key=api_key)

    assert api_key not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
