"""Performance regression tests for ``RecapClient.set_namespace``.

``set_namespace`` must short-circuit
when the requested campaign is already active, avoiding a redundant DB
round-trip (transaction open + namespace lookup + commit).
"""

from recap.client.base_client import RecapClient

from .conftest import count_statements


def test_set_namespace_same_id_issues_no_sql(apply_migrations, db_path):
    """Re-activating the already-active campaign must not touch the DB."""
    with RecapClient.from_sqlite(db_path) as client:
        client.create_namespace("perf")
        active_id = client.namespace_context.id

        with count_statements(client) as counter:
            client.set_namespace(active_id)

        assert counter["n"] == 0
        assert client.namespace_context.id == active_id


def test_set_namespace_different_id_still_queries(apply_migrations, db_path):
    """Switching to a different campaign must still load it from the DB."""
    with RecapClient.from_sqlite(db_path) as client:
        client.create_namespace("perf-a")
        id_a = client.namespace_context.id
        client.create_namespace("perf-b")

        # Active campaign is B; switching to A must run SQL.
        with count_statements(client) as counter:
            client.set_namespace(id_a)

        assert counter["n"] > 0
        assert client.namespace_context.id == id_a


def test_set_namespace_context_is_exposed(apply_migrations, db_path):
    """Passing the active campaign as a schema also short-circuits."""
    with RecapClient.from_sqlite(db_path) as client:
        active = client.create_namespace("perf-schema")

        with count_statements(client) as counter:
            client.set_namespace(active.id)

        assert counter["n"] == 0
        assert client.namespace_context.id == active.id


def test_set_namespace_force_reloads_issues_sql(apply_migrations, db_path):
    """``force=True`` must re-query the active campaign (escape hatch for
    out-of-band edits), even though the short-circuit would otherwise skip
    the round-trip."""
    with RecapClient.from_sqlite(db_path) as client:
        client.create_namespace("perf-force")
        active_id = client.namespace_context.id

        with count_statements(client) as counter:
            client.set_namespace(active_id, force=True)

        assert counter["n"] > 0
        assert client.namespace_context.id == active_id
