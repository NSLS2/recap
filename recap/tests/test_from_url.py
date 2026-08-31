"""Tests for RecapClient.from_url()."""

import inspect
from unittest.mock import patch
from uuid import uuid4

import pytest

from recap.schemas.namespace import NamespaceContext


def _namespace_context(path: str = "") -> NamespaceContext:
    return NamespaceContext(id=uuid4(), path=path)


def test_from_url_returns_root_recap_client():
    from recap.adapter.rest import RESTAdapter
    from recap.client import RecapClient

    with patch.object(
        RESTAdapter,
        "get_namespace_context",
        return_value=_namespace_context(),
    ):
        client = RecapClient.from_url("http://localhost:8000", api_key="secret")

    assert isinstance(client, RecapClient)
    assert client.namespace_path == ""
    client.close()


def test_from_url_accepts_initial_namespace_scope():
    from recap.adapter.rest import RESTAdapter
    from recap.client import RecapClient

    with patch.object(
        RESTAdapter,
        "get_namespace_context",
        return_value=_namespace_context("beamline/amx"),
    ):
        remote = RecapClient.from_url(
            "http://localhost:8000", api_key="secret", namespace="beamline/amx"
        )

    assert remote.namespace_path == "beamline/amx"
    remote.close()


def test_constructor_has_no_database_url_argument():
    from recap.client import RecapClient

    assert "url" not in inspect.signature(RecapClient).parameters


def test_from_url_wires_one_rest_adapter_without_db_discovery():
    from recap.adapter.rest import RESTAdapter
    from recap.client import RecapClient
    from recap.client.backend import ClientBackend

    with patch.object(
        RESTAdapter,
        "get_namespace_context",
        return_value=_namespace_context(),
    ) as get_namespace_context:
        client = RecapClient.from_url(
            "http://localhost:8000", api_key="secret", timeout=12.5
        )

    backend = client.connection_state.backend
    assert isinstance(backend, ClientBackend)
    assert isinstance(backend.reader, RESTAdapter)
    assert isinstance(backend.writer, RESTAdapter)
    assert backend.namespaces is backend.writer
    assert backend.namespace_writer is backend.writer
    assert backend.permissions is backend.reader
    assert backend.context_resolver is backend.reader
    reader = backend.reader
    writer = backend.writer
    assert reader._transport is writer._transport
    assert reader._transport._client.timeout.connect == 12.5
    assert "secret" not in repr(reader)
    assert "secret" not in repr(writer)
    get_namespace_context.assert_called_once_with("")
    client.close()


def test_from_url_query_maker_uses_client_backend_facade():
    from recap.adapter.rest import RESTAdapter
    from recap.client import RecapClient

    with patch.object(
        RESTAdapter,
        "get_namespace_context",
        return_value=_namespace_context("test"),
    ):
        client = RecapClient.from_url("http://localhost:8000", api_key="secret")
        qm = client.namespace("test").query_maker()

    assert qm.backend is client.connection_state.backend
    assert isinstance(qm.backend.reader, RESTAdapter)
    client.close()


def test_local_composition_uses_one_adapter_for_required_capabilities(tmp_path):
    from recap.adapter.local import LocalBackend
    from recap.client import RecapClient

    with RecapClient.from_sqlite(tmp_path / "recap.db") as client:
        backend = client.connection_state.backend
        assert isinstance(backend.reader, LocalBackend)
        assert backend.reader is backend.writer
        assert backend.reader is backend.namespaces
        assert backend.reader is backend.namespace_writer


def test_local_permissions_are_unsupported(tmp_path):
    from recap.client import RecapClient

    with RecapClient.from_sqlite(tmp_path / "recap.db") as client:
        assert client.connection_state.backend.permissions is None
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
    from recap.adapter.rest import RESTAdapter
    from recap.client import RecapClient

    with (
        patch.object(
            RESTAdapter,
            "get_namespace_context",
            return_value=_namespace_context(),
        ),
        patch("recap.client.base_client.apply_migrations") as migrations,
        patch("recap.client.base_client.create_engine") as create_engine,
        patch("recap.client.base_client.sessionmaker") as sessionmaker,
    ):
        client = RecapClient.from_url("http://localhost:8000", api_key="secret")

    migrations.assert_not_called()
    create_engine.assert_not_called()
    sessionmaker.assert_not_called()
    client.close()
