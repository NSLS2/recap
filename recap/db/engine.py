from __future__ import annotations

from pathlib import Path
from typing import Protocol

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

SQLITE_BUSY_TIMEOUT_MS = 5000


class DatabaseConfiguration(Protocol):
    @property
    def database_url(self) -> str: ...


def create_engine_and_session_factory(
    config: DatabaseConfiguration,
) -> tuple[Engine, sessionmaker]:
    """Build an engine and session factory tuned for selected database."""

    database_url = config.database_url
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        engine = create_engine(
            url,
            connect_args={
                "check_same_thread": False,
                "timeout": SQLITE_BUSY_TIMEOUT_MS / 1000,
            },
        )

        @event.listens_for(engine, "connect")
        def set_sqlite_busy_timeout(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            finally:
                cursor.close()

    elif url.get_backend_name() == "postgresql":
        if url.drivername == "postgresql":
            url = url.set(drivername="postgresql+psycopg")
        engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
    else:
        raise ValueError(f"Unsupported database dialect: {url.get_backend_name()}")

    return engine, sessionmaker(bind=engine, expire_on_commit=False)
