from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from functools import partial
from pathlib import Path
from shutil import copy2
from uuid import uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from recap.adapter.http_transport import HTTPTransport
from recap.adapter.rest import RESTAdapter
from recap.client.backend import ClientBackend
from recap.client.base_client import RecapClient
from recap.db.base import Base
from recap.db.namespace import Namespace
from recap.server.app import create_app
from recap.utils.general import Direction
from recap.utils.migrations import apply_migrations as upgrade_database


def _loopback_request(
    app_client: TestClient, request: httpx2.Request
) -> httpx2.Response:
    response = app_client.request(
        request.method,
        str(request.url),
        headers=dict(request.headers),
        content=request.content,
    )
    return httpx2.Response(
        response.status_code,
        headers=response.headers,
        content=response.content,
        request=request,
    )


def _loopback_transport(app_client: TestClient) -> httpx2.MockTransport:
    return httpx2.MockTransport(partial(_loopback_request, app_client))


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


@pytest.fixture
def api_client(tmp_path):
    with TestClient(create_app(tmp_path / "rest.db", api_key="secret")) as client:
        yield client


@pytest.fixture
def auth_header():
    return {"Authorization": "Apikey secret"}


@pytest.fixture
def rest_loopback_client(tmp_path):
    """Build remote client over FastAPI TestClient transport, not HTTP mocks."""
    db_path = tmp_path / "rest-loopback.db"
    api_key = "loopback-secret"
    with TestClient(create_app(db_path, api_key=api_key)) as app_client:
        transport = HTTPTransport(api_key)
        transport._client.close()
        transport._client = httpx2.Client(transport=_loopback_transport(app_client))
        rest = RESTAdapter("http://recap.test", _transport=transport)
        backend = ClientBackend(
            reader=rest,
            writer=rest,
            namespaces=rest,
            namespace_writer=rest,
            context_resolver=rest,
            permissions=rest,
        )
        with RecapClient._from_backends(backend, namespace="") as client:
            yield client


@pytest.fixture
def idempotency_headers(auth_header):
    def make(key, **extra):
        return {**auth_header, "Idempotency-Key": key, **extra}

    return make


@pytest.fixture
def create_namespace(api_client, idempotency_headers):
    def create(path="beamline/amx", metadata=None):
        metadata = {} if metadata is None else metadata
        parts = path.split("/")
        prefix = []
        for index, part in enumerate(parts):
            prefix.append(part)
            response = api_client.put(
                f"/api/v1/namespaces/{'/'.join(prefix)}",
                headers=idempotency_headers(f"namespace-{index}"),
                json={"metadata": metadata if index == len(parts) - 1 else {}},
            )
            assert response.status_code == 201
        return response

    return create


@pytest.fixture(params=["local", "remote"], ids=["local", "remote"])
def read_client(request, read_client_pair):
    local, remote = read_client_pair
    return local if request.param == "local" else remote


@pytest.fixture(scope="session")
def blank_database_path(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("seed-databases") / "blank-migrated.db"
    with RecapClient.from_sqlite(path):
        pass
    return path


@pytest.fixture
def copy_database() -> Callable[[Path, Path], Path]:
    def copy(source: Path, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        copy2(source, destination)
        return destination

    return copy


def _seed_query_namespace(client: RecapClient) -> str:
    return client.create_namespace("test/namespace").path


def _seed_query_resource_tree(client: RecapClient) -> str:
    namespace_path = client.create_namespace("test/resource-tree").path
    with client.build_resource_template(
        name="Parent", type_names=["container"]
    ) as builder:
        builder.close_child()
    with client.build_resource_template(name="Child", type_names=["sample"]) as builder:
        builder.close_child()
    with client.build_resource("root", "Parent") as builder:
        nested = builder.add_child("nested", "Child").resource
    root = client.get_resource("root", "Parent")
    client.build_resource(resource_id=nested.id).activate()
    client.build_resource(resource_id=root.id).activate()
    return namespace_path


def _seed_parity_graph(client: RecapClient) -> str:
    namespace_path = "test/mx-parity"
    client.create_namespace(namespace_path, metadata={"beamline": "AMX"})
    client = client.namespace(namespace_path)
    with client.build_resource_template(
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

    first_plate = client.create_resource("plate-1", "Parity plate")
    second_plate = client.create_resource("plate-2", "Parity plate")
    for plate, rating in ((first_plate, 12), (second_plate, 3)):
        with client.build_resource(resource_id=plate.id) as builder:
            model = builder.get_model()
            model.properties.metrics.rating = rating
            builder.set_model(model)
    sample = client.create_resource("sample-1", "sample", parent=first_plate)
    client.build_resource(resource_id=sample.id).activate()
    client.build_resource(resource_id=first_plate.id).activate()
    client.build_resource(resource_id=second_plate.id).activate()

    with client.build_process_template("Parity workflow", "1.0") as template:
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
        with client.build_process_run(
            name, "REST parity run", "Parity workflow", "1.0"
        ) as run:
            run.assign_resource("plate", plate)
            parameters = run.get_params("Collect")
            parameters.exposure.dwell = dwell
            run.set_params(parameters)
        run.finalize()

    return namespace_path


@pytest.fixture(scope="session")
def integration_seed_path(blank_database_path, tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("seed-databases") / "integration-seed.db"
    copy2(blank_database_path, path)
    with RecapClient.from_sqlite(path) as client:
        client.create_namespace("test")
        _seed_query_namespace(client)
        _seed_query_resource_tree(client)
        _seed_parity_graph(client)
    return path


@pytest.fixture
def integration_database_path(integration_seed_path, copy_database, tmp_path) -> Path:
    return copy_database(integration_seed_path, tmp_path / "test.db")


@pytest.fixture
def query_namespace_path() -> str:
    return "test/namespace"


@pytest.fixture
def query_resource_tree_path() -> str:
    return "test/resource-tree"


@pytest.fixture
def read_client_pair(integration_seed_path, copy_database, tmp_path):
    """Create isolated local and remote clients over a composite seed copy."""
    db_path = copy_database(integration_seed_path, tmp_path / "parity.db")
    with ExitStack() as stack:
        local = stack.enter_context(
            RecapClient.from_sqlite(db_path, namespace="test/mx-parity")
        )
        api_key = "parity-secret"
        app_client = stack.enter_context(
            TestClient(create_app(db_path, api_key=api_key))
        )

        transport = HTTPTransport(api_key)
        transport._client.close()
        transport._client = httpx2.Client(transport=_loopback_transport(app_client))
        rest = RESTAdapter("http://recap.test", _transport=transport)
        remote = stack.enter_context(
            RecapClient._from_backends(
                ClientBackend(
                    reader=rest,
                    writer=rest,
                    namespaces=rest,
                    namespace_writer=rest,
                    context_resolver=rest,
                    permissions=rest,
                ),
                namespace="test/mx-parity",
            )
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


@pytest.fixture
def client(blank_database_path, copy_database, tmp_path):
    db_path = copy_database(blank_database_path, tmp_path / "client.db")
    with RecapClient.from_sqlite(db_path) as recap_client:
        yield recap_client
