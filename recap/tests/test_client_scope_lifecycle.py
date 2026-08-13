import inspect

import pytest

from recap.client.base_client import RecapClient
from recap.client.connection_state import _ConnectionState


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


def test_shared_state_closes_backends_only_after_last_view_releases():
    backend = FakeClosable()
    state = _ConnectionState(backend=backend)

    state.acquire()
    state.acquire()
    state.release()

    assert backend.closed is False

    state.release()

    assert backend.closed is False


def test_shared_state_release_is_idempotent_after_close():
    backend = FakeClosable()
    state = _ConnectionState(backend=backend)
    state.acquire()
    state.release()
    state.release()

    assert state.closed is True
    assert backend.close_calls == 0


def test_shared_state_rejects_acquisition_after_close():
    state = _ConnectionState(backend=FakeClosable())
    state.acquire()
    state.release()

    with pytest.raises(RuntimeError):
        state.acquire()


def test_shared_state_disposes_optional_engine_when_last_view_releases():
    engine = FakeEngine()
    state = _ConnectionState(
        backend=FakeClosable(), engine=engine
    )
    state.acquire()
    state.release()
    state.release()

    assert engine.dispose_calls == 1


def test_shared_state_does_not_close_backend():
    backend = FakeClosable()
    state = _ConnectionState(backend=backend)
    state.acquire()
    state.release()

    assert backend.close_calls == 0


def test_connection_state_stores_single_backend():
    backend = object()

    state = _ConnectionState(backend=backend)

    assert state.backend is backend
    assert not hasattr(state, "read_backend")
    assert not hasattr(state, "write_backend")


def test_namespace_returns_same_type_and_shares_connection_state(tmp_path):
    root = RecapClient.from_sqlite(tmp_path / "recap.db")
    scoped = root.namespace("beamline/amx")

    assert isinstance(scoped, RecapClient)
    assert scoped is not root
    assert scoped.namespace_path == "beamline/amx"
    assert scoped.backend is root.backend

    scoped.close()
    assert root.backend is not None
    root.close()


def test_scoped_close_does_not_close_shared_backend_until_last_view(tmp_path):
    root = RecapClient.from_sqlite(tmp_path / "recap.db")
    scoped = root.namespace("beamline/amx")
    state = root._connection_state

    scoped.close()

    assert state.closed is False
    assert state._active_views == 1

    root.close()

    assert state.closed is True


def test_namespace_normalizes_root_and_slashes(tmp_path):
    root = RecapClient.from_sqlite(tmp_path / "recap.db")

    assert root.namespace("/").namespace_path == ""
    assert root.namespace("/beamline/amx/").namespace_path == "beamline/amx"

    root.close()


def test_namespace_key_access_is_additive(tmp_path):
    root = RecapClient.from_sqlite(tmp_path / "recap.db", namespace="beamline")

    scoped = root["amx"]["/proposal"]

    assert isinstance(scoped, RecapClient)
    assert scoped.namespace_path == "beamline/amx/proposal"
    assert scoped.backend is root.backend

    root.close()


def test_namespace_root_key_preserves_current_scope(tmp_path):
    root = RecapClient.from_sqlite(tmp_path / "recap.db", namespace="beamline")

    scoped = root["/"]

    assert scoped.namespace_path == "beamline"
    scoped.close()
    root.close()


def test_namespace_key_requires_string(tmp_path):
    root = RecapClient.from_sqlite(tmp_path / "recap.db")

    with pytest.raises(TypeError, match="namespace key must be a string"):
        root[123]

    root.close()


def test_scoped_view_context_manager_returns_scoped_view(tmp_path):
    root = RecapClient.from_sqlite(tmp_path / "recap.db")
    scoped = root.namespace("beamline/amx")

    with scoped as entered:
        assert entered is scoped
        assert entered.namespace_path == "beamline/amx"

    assert root.backend is not None
    root.close()


def test_scoped_view_close_is_idempotent(tmp_path):
    root = RecapClient.from_sqlite(tmp_path / "recap.db")
    scoped = root.namespace("beamline/amx")

    scoped.close()
    scoped.close()

    assert root._connection_state is not None
    assert root._connection_state._active_views == 1
    root.close()


def test_scoped_local_builder_resolves_namespace_path_without_active_context(tmp_path):
    root = RecapClient.from_sqlite(tmp_path / "recap.db")
    root.create_namespace("beamline")
    root.create_namespace("beamline/amx")
    root._namespace_context = None
    scoped = root.namespace("beamline/amx")

    builder = scoped.build_resource_template(name="Sample", type_names=["sample"])

    assert builder.namespace_id

    scoped.close()
    root.close()


def test_scoped_local_get_resource_resolves_namespace_without_active_context(tmp_path):
    root = RecapClient.from_sqlite(tmp_path / "recap.db")
    root.create_namespace("beamline")
    root.create_namespace("beamline/amx")
    with root.build_resource_template(name="Sample", type_names=["sample"]):
        pass
    root.create_resource("S-001", "Sample")
    root._namespace_context = None
    scoped = root.namespace("beamline/amx")

    resource = scoped.get_resource("S-001", "Sample")

    assert resource.name == "S-001"

    scoped.close()
    root.close()


def test_scoped_query_does_not_require_namespace_argument():
    client = RecapClient.from_url(
        "http://recap.test", api_key="secret", namespace="beamline/amx"
    )

    query = client.query_maker()

    assert query.namespace_path == "beamline/amx"
    client.close()


def test_scoped_client_rejects_redundant_namespace_override():
    client = RecapClient.from_url(
        "http://recap.test", api_key="secret", namespace="beamline/amx"
    )

    with pytest.raises(TypeError):
        client.query_maker(namespace="other")

    client.close()


def test_scoped_builder_signature_has_no_namespace_path_argument():
    parameters = inspect.signature(RecapClient.build_resource).parameters

    assert "namespace_path" not in parameters
