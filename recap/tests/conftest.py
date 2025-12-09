import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from recap.client.base_client import RecapClient
from recap.db.base import Base
from recap.utils.migrations import apply_migrations as upgrade_database
from recap.utils.migrations import downgrade_migrations


@pytest.fixture(scope="session")
def db_url(tmp_path_factory):
    db_dir = tmp_path_factory.mktemp("data")
    db_path = db_dir / "test.db"
    return f"sqlite:///{db_path}"


@pytest.fixture(scope="session")
def apply_migrations(db_url):
    """
    Run alembic migrations once before all tests.
    """
    upgrade_database(db_url)

    yield

    # Optional cleanup for SQLite or temporary DB
    downgrade_migrations(db_url)


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
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="function")
def db_session(apply_migrations, engine):
    """Create a new database session"""
    connection = engine.connect()
    transaction = connection.begin()
    TestingSessionLocal = sessionmaker(bind=connection)
    session = TestingSessionLocal(bind=connection)
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
    session.close()
    connection.close()


@pytest.fixture(scope="function")
def client(db_url, apply_migrations):
    # client = RecapClient(url=db_url)
    # try:
    #     yield client
    # finally:
    #     client.close()
    with RecapClient(url=db_url) as client:
        yield client
