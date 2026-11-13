import os

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.pool import QueuePool

from recap.db.engine import create_engine_and_session_factory
from recap.server.config import ServerConfig


def test_db_path_selects_sqlite_and_sets_busy_timeout(tmp_path):
    config = ServerConfig(db_path=tmp_path / "recap.db", api_key="secret")
    engine, factory = create_engine_and_session_factory(config)

    assert config.database_url == f"sqlite:///{tmp_path / 'recap.db'}"
    assert factory.kw["bind"] is engine
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA busy_timeout")) == 5000
    engine.dispose()


def test_database_uri_selects_postgresql_and_uses_pooling():
    config = ServerConfig(
        database_uri="postgresql+psycopg://user:password@db.example/recap",
        api_key="secret",
    )
    engine, factory = create_engine_and_session_factory(config)

    assert config.database_url == (
        "postgresql+psycopg://user:password@db.example/recap"
    )
    assert isinstance(engine.pool, QueuePool)
    assert engine.pool._pre_ping is True
    assert factory.kw["bind"] is engine
    engine.dispose()


@pytest.mark.parametrize(
    "values",
    [
        {"api_key": "secret"},
        {
            "db_path": "/tmp/recap.db",
            "database_uri": "postgresql+psycopg://user:password@db/recap",
            "api_key": "secret",
        },
    ],
)
def test_exactly_one_database_location_is_required(values):
    with pytest.raises(ValidationError, match="exactly one"):
        ServerConfig(**values)


def test_database_uri_repr_redacts_credentials():
    config = ServerConfig(
        database_uri="postgresql+psycopg://user:never-print-this@db/recap",
        api_key="secret",
    )

    assert "never-print-this" not in repr(config)
    assert "never-print-this" not in str(config)


def test_server_migrates_selected_database_url(monkeypatch):
    from recap.server import app as app_module

    selected = "postgresql+psycopg://user:password@db/recap"
    migrated = []
    monkeypatch.setattr(app_module, "apply_migrations", migrated.append)
    monkeypatch.setattr(
        app_module,
        "create_engine_and_session_factory",
        lambda config: (object(), lambda: None),
    )

    app_module.create_app(database_uri=selected)

    assert migrated == [selected]


@pytest.mark.skipif(
    "RECAP_TEST_POSTGRES_URI" not in os.environ,
    reason="RECAP_TEST_POSTGRES_URI is not configured",
)
def test_postgresql_transaction_can_roll_back():
    config = ServerConfig(
        database_uri=os.environ["RECAP_TEST_POSTGRES_URI"], api_key="secret"
    )
    engine, factory = create_engine_and_session_factory(config)
    session = factory()
    transaction = session.begin()
    session.execute(text("SELECT 1"))
    transaction.rollback()
    assert not session.in_transaction()
    session.close()
    engine.dispose()
