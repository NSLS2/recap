from contextlib import contextmanager
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from recap.client.base_client import RecapClient
from recap.db.base import Base
from recap.db.namespace import Namespace
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
            client.set_namespace(existing_id)
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
