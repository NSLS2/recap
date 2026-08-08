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
    read = FakeClosable()
    write = FakeClosable()
    state = _ConnectionState(read_backend=read, write_backend=write)

    state.acquire()
    state.acquire()
    state.release()

    assert read.closed is False
    assert write.closed is False

    state.release()

    assert read.closed is True
    assert write.closed is True


def test_shared_state_release_is_idempotent_after_close():
    read = FakeClosable()
    write = FakeClosable()
    state = _ConnectionState(read_backend=read, write_backend=write)
    state.acquire()
    state.release()
    state.release()

    assert state.closed is True
    assert read.close_calls == 1
    assert write.close_calls == 1


def test_shared_state_rejects_acquisition_after_close():
    state = _ConnectionState(
        read_backend=FakeClosable(), write_backend=FakeClosable()
    )
    state.acquire()
    state.release()

    with pytest.raises(RuntimeError):
        state.acquire()


def test_shared_state_disposes_optional_engine_when_last_view_releases():
    engine = FakeEngine()
    state = _ConnectionState(
        read_backend=FakeClosable(), write_backend=FakeClosable(), engine=engine
    )
    state.acquire()
    state.release()
    state.release()

    assert engine.dispose_calls == 1


def test_shared_state_closes_backend_shared_by_read_and_write_once():
    backend = FakeClosable()
    state = _ConnectionState(read_backend=backend, write_backend=backend)
    state.acquire()
    state.release()

    assert backend.close_calls == 1


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
