from contextlib import ExitStack, contextmanager
from uuid import uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from recap.client.base_client import RecapClient
from recap.db.base import Base
from recap.db.namespace import Namespace
from recap.server.app import create_app
from recap.utils.general import Direction
from recap.utils.migrations import apply_migrations as upgrade_database


@contextmanager
def count_statements(target):
    """Count SQL statements executed against ``target`` within the block.

    ``target`` may be an :class:`~sqlalchemy.engine.Engine`, a
    :class:`~sqlalchemy.engine.Connection`, or any object exposing an
    ``engine`` attribute (e.g. a :class:`RecapClient`).  Yields a mutable
    counter dict with a single ``"n"`` key so callers can assert a bounded
    statement count and verify that N+1 / redundant-round-trip regressions
    have not crept back in.

    Example::

         with count_statements(client) as counter:
             client.namespace("existing")
        assert counter["n"] == 0
    """
    engine = getattr(target, "engine", target)
    counter = {"n": 0}

    def _before(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _before)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", _before)


@pytest.fixture
def statement_counter():
    """Fixture exposing :func:`count_statements` for use in tests."""
    return count_statements


@pytest.fixture(params=["local", "remote"], ids=["local", "remote"])
def read_client(request, read_client_pair):
    local, remote = read_client_pair
    return local if request.param == "local" else remote


@pytest.fixture
def read_client_pair(tmp_path, monkeypatch):
    """Create equivalent local and remote clients over one prepared database."""
    db_path = tmp_path / "parity.db"
    with ExitStack() as stack:
        local = stack.enter_context(RecapClient.from_sqlite(db_path))
        local.create_namespace("test")
        local.create_namespace("test/mx-parity", metadata={"beamline": "AMX"})

        with local.build_resource_template(
            name="Parity plate", type_names=["container", "plate"]
        ) as template:
            template.add_properties(
                {"metrics": [{"name": "rating", "type": "int", "default": 1}]}
            )
            (
                template.add_child("sample", ["sample"])
                .add_properties(
                    {"contents": [{"name": "mass", "type": "float", "default": 2.5}]}
                )
                .close_child()
            )
        template.activate()

        first_plate = local.create_resource("plate-1", "Parity plate")
        second_plate = local.create_resource("plate-2", "Parity plate")
        for plate, rating in ((first_plate, 12), (second_plate, 3)):
            with local.build_resource(resource_id=plate.id) as builder:
                model = builder.get_model()
                model.properties.metrics.rating = rating
                builder.set_model(model)
        sample = local.create_resource("sample-1", "sample", parent=first_plate)
        local.build_resource(resource_id=sample.id).activate()
        local.build_resource(resource_id=first_plate.id).activate()
        local.build_resource(resource_id=second_plate.id).activate()

        with local.build_process_template("Parity workflow", "1.0") as template:
            template.add_resource_slot("plate", "container", Direction.input)
            (
                template.add_step("Collect")
                .add_parameters(
                    {"exposure": [{"name": "dwell", "type": "int", "default": 1}]}
                )
                .bind_slot("source", "plate")
                .close_step()
            )
        template.activate()

        for name, plate, dwell in (
            ("run-high", first_plate, 15),
            ("run-low", second_plate, 5),
        ):
            with local.build_process_run(
                name, "GraphQL parity run", "Parity workflow", "1.0"
            ) as run:
                run.assign_resource("plate", plate)
                parameters = run.get_params("Collect")
                parameters.exposure.dwell = dwell
                run.set_params(parameters)
            run.finalize()

        api_key = "parity-secret"
        app_client = stack.enter_context(TestClient(create_app(db_path, api_key=api_key)))

        def post(_client, url, *, json, **kwargs):
            assert url.endswith("/graphql")
            return app_client.post("/graphql", json=json, **kwargs)

        monkeypatch.setattr(httpx2.Client, "post", post)
        remote = stack.enter_context(
            RecapClient.from_url("http://recap.test", api_key=api_key)
        )
        yield local, remote


@pytest.fixture
def parity_clients(read_client_pair):
    """Preserve paired-client fixture name for parity tests."""
    return read_client_pair


@pytest.fixture(scope="session")
def db_path(tmp_path_factory):
    db_dir = tmp_path_factory.mktemp("data")
    return db_dir / "test.db"


@pytest.fixture(scope="session")
def db_url(db_path):
    return f"sqlite:///{db_path}"


@pytest.fixture(scope="session")
def apply_migrations(db_url):
    """
    Run Alembic migrations once before all tests.
    """
    upgrade_database(db_url)

    yield


@pytest.fixture(scope="session")
def engine(db_url):
    engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def setup_database(engine):
    """Create all tables"""
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture(scope="function")
def db_session(apply_migrations, engine):
    """Create a new database session"""
    connection = engine.connect()
    transaction = connection.begin()
    TestingSessionLocal = sessionmaker(bind=connection)
    session = TestingSessionLocal(bind=connection)
    default_namespace = Namespace(path=f"test/{uuid4()}", metadata_json={})
    session.add(default_namespace)

    def assign_namespace(session, flush_context, instances):
        for obj in session.new:
            if not hasattr(obj, "namespace_id") or obj.namespace_id is not None:
                continue
            if getattr(obj, "namespace", None) is not None:
                continue
            parent = getattr(obj, "parent", None)
            obj.namespace = (
                parent.namespace if parent is not None else default_namespace
            )

    event.listen(session, "before_flush", assign_namespace)
    # Add default start and end actionTypes
    """
    start_action_type = StepTemplate(name="Start")
    end_action_type = StepTemplate(name="End")
    session.add(start_action_type)
    session.add(end_action_type)
    session.commit()
    """
    yield session

    if transaction.is_active:
        transaction.rollback()
    event.remove(session, "before_flush", assign_namespace)
    session.close()
    connection.close()


@pytest.fixture
def namespaced_session(db_session):
    """Adapt legacy graph fixtures by assigning one explicit namespace."""
    namespace = Namespace(path=f"test/{uuid4()}", metadata_json={})
    db_session.add(namespace)

    def assign_namespace(session, flush_context, instances):
        for obj in session.new:
            if hasattr(obj, "namespace_id"):
                obj.namespace = namespace

    event.listen(db_session, "before_flush", assign_namespace)
    yield db_session
    event.remove(db_session, "before_flush", assign_namespace)


@pytest.fixture(scope="function")
def client(db_path, apply_migrations):
    with RecapClient.from_sqlite(db_path) as client:
        parent_path = f"test-{uuid4()}"
        client.create_namespace(parent_path)
        client.create_namespace(f"{parent_path}/{uuid4()}")
        yield client
