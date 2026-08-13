"""Tests for RecapClient.from_url()."""

import inspect
from unittest.mock import patch

import pytest


def test_from_url_returns_root_recap_client():
    from recap.client import RecapClient

    client = RecapClient.from_url("http://localhost:8000", api_key="secret")

    assert isinstance(client, RecapClient)
    assert client.namespace_path == ""
    client.close()


def test_from_url_accepts_initial_namespace_scope():
    from recap.client import RecapClient

    remote = RecapClient.from_url(
        "http://localhost:8000", api_key="secret", namespace="beamline/amx"
    )

    assert remote.namespace_path == "beamline/amx"
    remote.close()


def test_constructor_has_no_database_url_argument():
    from recap.client import RecapClient

    assert "url" not in inspect.signature(RecapClient).parameters


def test_from_url_wires_graphql_reads_and_rest_writes_without_db_discovery():
    from recap.adapter.graphql import GraphQLAdapter
    from recap.adapter.rest import RESTAdapter
    from recap.client import RecapClient
    from recap.client.backend import ClientBackend

    with patch("httpx2.get") as get:
        client = RecapClient.from_url(
            "http://localhost:8000", api_key="secret", timeout=12.5
        )

    assert isinstance(client.backend, ClientBackend)
    assert isinstance(client.backend.reader, GraphQLAdapter)
    assert isinstance(client.backend.writer, RESTAdapter)
    assert client.backend.namespaces is client.backend.writer
    assert client.backend.namespace_writer is client.backend.writer
    assert client.backend.permissions is client.backend.reader
    assert client.backend.context_resolver is None
    assert client.backend.reader._headers.as_dict() == {"Authorization": "Apikey secret"}
    assert client.backend.writer._auth.headers() == {"Authorization": "Apikey secret"}
    assert client.backend.writer._client.timeout.connect == 12.5
    get.assert_not_called()
    client.close()


def test_from_url_query_maker_uses_client_backend_facade():
    from recap.adapter.graphql import GraphQLAdapter
    from recap.client import RecapClient

    client = RecapClient.from_url("http://localhost:8000", api_key="secret")
    qm = client.namespace("test").query_maker()

    assert qm.backend is client.backend
    assert isinstance(qm.backend.reader, GraphQLAdapter)
    client.close()


def test_local_composition_uses_one_adapter_for_required_capabilities(tmp_path):
    from recap.adapter.local import LocalBackend
    from recap.client import RecapClient

    with RecapClient.from_sqlite(tmp_path / "recap.db") as client:
        backend = client.backend
        assert isinstance(backend.reader, LocalBackend)
        assert backend.reader is backend.writer
        assert backend.reader is backend.namespaces
        assert backend.reader is backend.namespace_writer


def test_local_permissions_are_unsupported(tmp_path):
    from recap.client import RecapClient

    with RecapClient.from_sqlite(tmp_path / "recap.db") as client:
        assert client.backend.permissions is None
        with pytest.raises(RuntimeError, match="Permissions"):
            client.permissions()


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
    ):
        client = RecapClient.from_url("http://localhost:8000", api_key="secret")

    migrations.assert_not_called()
    create_engine.assert_not_called()
    sessionmaker.assert_not_called()
    client.close()
