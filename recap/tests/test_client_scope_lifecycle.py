import pytest

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
