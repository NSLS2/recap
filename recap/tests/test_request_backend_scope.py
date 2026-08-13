from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from recap.adapter.local import LocalBackend
from recap.server.dependencies import get_local_backend


class TrackingSession(Session):
    closed = False

    def close(self):
        self.closed = True
        super().close()


def test_each_request_gets_independent_backend_without_persistent_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, class_=TrackingSession)
    app = FastAPI()
    app.state.session_factory = factory
    observed = []
    observed_lock = Lock()

    @app.get("/scope")
    def scope(backend: LocalBackend = Depends(get_local_backend)):  # noqa: B008
        with observed_lock:
            observed.append(backend)
        return {"backend": id(backend), "has_session": hasattr(backend, "_session")}

    with (
        TestClient(app) as client,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        responses = list(executor.map(lambda _: client.get("/scope"), range(2)))

    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["backend"] for response in responses}) == 2
    assert all(not response.json()["has_session"] for response in responses)
    assert all(not hasattr(backend, "_session") for backend in observed)
    engine.dispose()


def test_request_backend_has_no_session_when_handler_raises():
    engine = create_engine("sqlite://")
    factory = sessionmaker(bind=engine, class_=TrackingSession)
    app = FastAPI()
    app.state.session_factory = factory
    observed = []

    @app.get("/failure")
    def failure(backend: LocalBackend = Depends(get_local_backend)):  # noqa: B008
        observed.append(backend)
        raise RuntimeError("boom")

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/failure").status_code == 500

    assert not hasattr(observed[0], "_session")
    engine.dispose()


def test_server_app_stores_session_factory_instead_of_shared_backend(tmp_path):
    from recap.server.app import create_app

    app = create_app(tmp_path / "server.db")

    assert hasattr(app.state, "session_factory")
    assert not hasattr(app.state, "backend")
    assert not hasattr(app.state, "client")
