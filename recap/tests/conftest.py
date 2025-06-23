import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from recap.models.base import Base

TEST_DATABASE_URL = "sqlite+pysqlite:///:memory:"


@pytest.fixture(scope="session")
def engine():
    engine = create_engine(
        TEST_DATABASE_URL,
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
def db_session(engine, setup_database):
    """Create a new database session"""
    connection = engine.connect()
    transcaction = connection.begin()
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

    session.close()
    transcaction.rollback()
    connection.close()
