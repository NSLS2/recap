import inspect
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

import pytest

from recap.client.base_client import RecapClient
from recap.client.connection_state import ConnectionState
from recap.schemas.namespace import NamespaceContext


class FakeClosable:
    def __init__(self):
        self.closed = False
        self.close_calls = 0

    def close(self):
        self.closed = True
        self.close_calls += 1


class FakeEngine:
    def __init__(self):
        self.dispose_calls = 0

    def dispose(self):
        self.dispose_calls += 1


class FlakyBackend:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        if self.close_calls == 1:
            raise RuntimeError("backend close failed")


def test_shared_state_closes_backend_only_after_last_view_releases():
    backend = FakeClosable()
    state = ConnectionState(backend=backend)

    state.acquire()
    state.acquire()
    state.release()

    assert backend.closed is False

    state.release()

    assert backend.closed is True


def test_shared_state_release_is_idempotent_after_close():
    backend = FakeClosable()
    state = ConnectionState(backend=backend)
    state.acquire()
    state.release()
    state.release()

    assert state.closed is True
    assert backend.close_calls == 1


def test_shared_state_rejects_acquisition_after_close():
    state = ConnectionState(backend=FakeClosable())
    state.acquire()
    state.release()

    with pytest.raises(RuntimeError):
        state.acquire()


def test_shared_state_disposes_optional_engine_when_last_view_releases():
    engine = FakeEngine()
    state = ConnectionState(backend=FakeClosable(), engine=engine)
    state.acquire()
    state.release()
    state.release()

    assert engine.dispose_calls == 1


def test_shared_state_closes_backend():
    backend = FakeClosable()
    state = ConnectionState(backend=backend)
    state.acquire()
    state.release()

    assert backend.close_calls == 1


def test_shared_state_retries_final_backend_cleanup_after_failure():
    backend = FlakyBackend()
    state = ConnectionState(backend=backend)
    state.acquire()

    with pytest.raises(RuntimeError, match="backend close failed"):
        state.release()

    assert state.closed is False
    assert state._active_views == 0

    state.close()

    assert state.closed is True
    assert backend.close_calls == 2


def test_connection_state_stores_single_backend():
    backend = object()

    state = ConnectionState(backend=backend)

    assert state.backend is backend
    assert not hasattr(state, "read_backend")
    assert not hasattr(state, "write_backend")


def test_namespace_returns_same_type_and_shares_connection_state(tmp_path):
    root = RecapClient.from_sqlite(tmp_path / "recap.db")
    root.create_namespace("beamline")
    root.create_namespace("beamline/amx")
    scoped = root.namespace("beamline/amx")

    assert isinstance(scoped, RecapClient)
    assert scoped is not root
    assert scoped.namespace_path == "beamline/amx"
    assert scoped.connection_state.backend is root.connection_state.backend

    scoped.close()
    assert root.connection_state.backend is not None
    root.close()


def test_scoped_close_does_not_close_shared_backend_until_last_view(tmp_path):
    root = RecapClient.from_sqlite(tmp_path / "recap.db")
    root.create_namespace("beamline")
    root.create_namespace("beamline/amx")
    scoped = root.namespace("beamline/amx")
    state = root.connection_state

    scoped.close()

    assert state.closed is False
    assert state._active_views == 1

    root.close()

    assert state.closed is True


def test_remote_transport_closes_once_after_last_view_releases():
    with patch(
        "recap.adapter.rest.RESTAdapter.get_namespace_context",
        lambda _adapter, path: NamespaceContext(id=uuid4(), path=path),
    ):
        root = RecapClient.from_url("http://recap.test", api_key="secret")
        scoped = root.namespace("beamline/amx")
    transport = root.connection_state.backend.reader._transport

    with patch.object(transport._client, "close") as close:
        scoped.close()
        close.assert_not_called()

        root.close()

    close.assert_called_once_with()


def test_namespace_normalizes_root_and_slashes(tmp_path):
    root = RecapClient.from_sqlite(tmp_path / "recap.db")
    root.create_namespace("beamline")
    root.create_namespace("beamline/amx")

    assert root.namespace("/").namespace_path == ""
    assert root.namespace("/beamline/amx/").namespace_path == "beamline/amx"

    root.close()


def test_namespace_key_access_is_additive(tmp_path):
    root = RecapClient.from_sqlite(tmp_path / "recap.db")
    root.create_namespace("beamline")
    root.create_namespace("beamline/amx")
    root.create_namespace("beamline/amx/proposal")

    root = RecapClient.from_sqlite(tmp_path / "recap.db", namespace="beamline")
    scoped = root["amx"]["/proposal"]

    assert isinstance(scoped, RecapClient)
    assert scoped.namespace_path == "beamline/amx/proposal"
    assert scoped.connection_state.backend is root.connection_state.backend

    root.close()


def test_namespace_root_key_preserves_current_scope(tmp_path):
    root = RecapClient.from_sqlite(tmp_path / "recap.db")
    root.create_namespace("beamline")
    root = RecapClient.from_sqlite(tmp_path / "recap.db", namespace="beamline")

    scoped = root["/"]

    assert scoped.namespace_path == "beamline"
    scoped.close()
    root.close()


def test_namespace_key_requires_string(tmp_path):
    root = RecapClient.from_sqlite(tmp_path / "recap.db")

    with pytest.raises(TypeError, match="namespace path must be a string"):
        root[123]

    root.close()


def test_scoped_view_context_manager_returns_scoped_view(tmp_path):
    root = RecapClient.from_sqlite(tmp_path / "recap.db")
    root.create_namespace("beamline")
    root.create_namespace("beamline/amx")
    scoped = root.namespace("beamline/amx")

    with scoped as entered:
        assert entered is scoped
        assert entered.namespace_path == "beamline/amx"

    assert root.connection_state.backend is not None
    root.close()


def test_scoped_view_close_is_idempotent(tmp_path):
    root = RecapClient.from_sqlite(tmp_path / "recap.db")
    root.create_namespace("beamline")
    root.create_namespace("beamline/amx")
    scoped = root.namespace("beamline/amx")

    scoped.close()
    scoped.close()

    assert root.connection_state is not None
    assert root.connection_state._active_views == 1
    root.close()


def test_concurrent_repeated_close_of_one_view_releases_one_reference(tmp_path):
    root = RecapClient.from_sqlite(tmp_path / "recap.db")
    root.create_namespace("beamline")
    root.create_namespace("beamline/amx")
    scoped = root.namespace("beamline/amx")
    state = root.connection_state

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _index: scoped.close(), range(32)))

    assert state._active_views == 1
    assert state.closed is False
    root.close()


def test_scoped_local_builder_resolves_namespace_path_without_active_context(tmp_path):
    root = RecapClient.from_sqlite(tmp_path / "recap.db")
    root.create_namespace("beamline")
    root.create_namespace("beamline/amx")
    scoped = root.namespace("beamline/amx")

    builder = scoped.build_resource_template(name="Sample", type_names=["sample"])

    assert builder.namespace_context.id

    scoped.close()
    root.close()


def test_scoped_local_get_resource_resolves_namespace_without_active_context(tmp_path):
    root = RecapClient.from_sqlite(tmp_path / "recap.db")
    root.create_namespace("beamline")
    root.create_namespace("beamline/amx")
    with root.build_resource_template(name="Sample", type_names=["sample"]):
        pass
    root.create_resource("S-001", "Sample")
    # root._namespace_context = None
    scoped = root.namespace("beamline/amx")

    resource = scoped.get_resource("S-001", "Sample")

    assert resource.name == "S-001"

    scoped.close()
    root.close()


def test_scoped_query_does_not_require_namespace_argument(monkeypatch):
    from uuid import UUID

    from recap.adapter.rest import RESTAdapter
    from recap.schemas.namespace import NamespaceContext

    monkeypatch.setattr(
        RESTAdapter,
        "get_namespace_context",
        lambda _adapter, path: NamespaceContext(id=UUID(int=0), path=path, metadata={}),
    )

    client = RecapClient.from_url(
        "http://recap.test", api_key="secret", namespace="beamline/amx"
    )

    query = client.query_maker()

    assert query.namespace_path == "beamline/amx"
    client.close()


def test_scoped_client_rejects_redundant_namespace_override(monkeypatch):
    from uuid import UUID

    from recap.adapter.rest import RESTAdapter
    from recap.schemas.namespace import NamespaceContext

    monkeypatch.setattr(
        RESTAdapter,
        "get_namespace_context",
        lambda _adapter, path: NamespaceContext(id=UUID(int=0), path=path, metadata={}),
    )

    client = RecapClient.from_url(
        "http://recap.test", api_key="secret", namespace="beamline/amx"
    )

    with pytest.raises(TypeError):
        client.query_maker(namespace="other")

    client.close()


def test_scoped_builder_signature_has_no_namespace_path_argument():
    parameters = inspect.signature(RecapClient.build_resource).parameters

    assert "namespace_path" not in parameters
