"""Optional PostgreSQL integration coverage for overlapping subtree writes."""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from recap.commands.errors import CommandConflictError
from recap.commands.service import CommandService
from recap.db.engine import create_engine_and_session_factory
from recap.server.config import ServerConfig


@pytest.mark.skipif(
    "RECAP_TEST_POSTGRES_URI" not in os.environ,
    reason="RECAP_TEST_POSTGRES_URI is not configured",
)
def test_overlapping_postgres_finalizations_do_not_deadlock():
    config = ServerConfig(
        database_uri=os.environ["RECAP_TEST_POSTGRES_URI"], api_key=uuid4().hex
    )
    engine, _ = create_engine_and_session_factory(config)
    # This test owns only its randomly named schema, never application tables.
    schema = f"finalize_test_{uuid4().hex}"
    namespace_id = uuid4()
    root_id, child_id, leaf_id = (UUID(int=value) for value in [4, 3, 1])
    schema_created = False
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(
                text(f'''
                CREATE TABLE "{schema}".resource (
                    id UUID PRIMARY KEY, parent_id UUID, namespace_id UUID NOT NULL,
                    status VARCHAR NOT NULL, revision INTEGER NOT NULL,
                    modified_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            )
            for resource_id, parent_id in [
                (root_id, None),
                (child_id, root_id),
                (leaf_id, child_id),
            ]:
                connection.execute(
                    text(f'''
                        INSERT INTO "{schema}".resource
                        (id, parent_id, namespace_id, status, revision)
                        VALUES (:id, :parent_id, :namespace_id, 'MUTABLE', 1)
                    '''),
                    {
                        "id": resource_id,
                        "parent_id": parent_id,
                        "namespace_id": namespace_id,
                    },
                )
        schema_created = True
        factory = sessionmaker(
            bind=engine.execution_options(schema_translate_map={None: schema})
        )
        ready = Barrier(2)

        def finalize(resource_id):
            with factory.begin() as session:
                session.execute(text("SET LOCAL lock_timeout = '5s'"))
                session.execute(text("SET LOCAL statement_timeout = '10s'"))
                ready.wait(timeout=10)
                try:
                    CommandService._finalize_resource_tree(
                        session,
                        SimpleNamespace(id=resource_id, namespace_id=namespace_id),
                        expected_revision=1,
                        depth=None,
                    )
                except CommandConflictError:
                    # The ancestor may have finalized this child first. A stale
                    # revision is expected, a database deadlock is not.
                    return "stale"
            return "finalized"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(finalize, [root_id, child_id]))
        assert outcomes[0] == "finalized"
        assert outcomes[1] in {"finalized", "stale"}
        with engine.connect() as connection:
            rows = connection.execute(
                text(f'SELECT status, revision FROM "{schema}".resource')
            ).all()
        assert rows == [("ACTIVE", 2)] * 3
    finally:
        if schema_created:
            with engine.begin() as connection:
                connection.execute(text(f'DROP TABLE IF EXISTS "{schema}".resource'))
                connection.execute(text(f'DROP SCHEMA "{schema}"'))
        engine.dispose()
