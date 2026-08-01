"""Tests for RecapClient.from_url()."""

from unittest.mock import patch

import pytest


def test_from_url_wires_graphql_reads_and_rest_writes_without_db_discovery():
    from recap.adapter.graphql import GraphQLAdapter
    from recap.adapter.rest import RESTAdapter
    from recap.client import RecapClient

    with patch("httpx2.get") as get:
        client = RecapClient.from_url(
            "http://localhost:8000", api_key="secret", timeout=12.5
        )

    assert isinstance(client._read_backend, GraphQLAdapter)
    assert isinstance(client.backend, RESTAdapter)
    assert client._read_backend._headers.as_dict() == {"Authorization": "Apikey secret"}
    assert client.backend._auth.headers() == {"Authorization": "Apikey secret"}
    assert client.backend._client.timeout.connect == 12.5
    get.assert_not_called()
    client.close()


def test_from_url_query_maker_uses_read_backend():
    from recap.adapter.graphql import GraphQLAdapter
    from recap.client import RecapClient

    client = RecapClient.from_url("http://localhost:8000", api_key="secret")
    qm = client.query_maker(namespace="test")

    assert isinstance(qm.backend, GraphQLAdapter)
    client.close()


def test_from_url_rejects_unscoped_remote_client_before_http():
    from recap.client import RecapClient

    with (
        patch("httpx2.get") as get,
        pytest.raises(ValueError, match="unscoped"),
    ):
        RecapClient.from_url("http://localhost:8000", api_key="secret", unscoped=True)

    get.assert_not_called()


def test_from_url_does_not_import_or_construct_sqlite_components():
    from recap.client import RecapClient

    with (
        patch("recap.client.base_client.apply_migrations") as migrations,
        patch("recap.client.base_client.create_engine") as create_engine,
        patch("recap.client.base_client.sessionmaker") as sessionmaker,
        patch("recap.client.base_client.LocalBackend") as local_backend,
    ):
        client = RecapClient.from_url("http://localhost:8000", api_key="secret")

    migrations.assert_not_called()
    create_engine.assert_not_called()
    sessionmaker.assert_not_called()
    local_backend.assert_not_called()
    client.close()
