"""Performance regression tests for ``RecapClient.set_campaign``.

See ``RECAP_QUERY_OPTIMIZATION.md`` Step C: ``set_campaign`` must short-circuit
when the requested campaign is already active, avoiding a redundant DB
round-trip (transaction open + ``SELECT Campaign`` + commit).
"""

from recap.client.base_client import RecapClient

from .conftest import count_statements


def test_set_campaign_same_id_issues_no_sql(apply_migrations, db_url):
    """Re-activating the already-active campaign must not touch the DB."""
    with RecapClient(url=db_url) as client:
        client.create_campaign("perf-camp", "perf-prop")
        active_id = client._campaign.id

        with count_statements(client) as counter:
            client.set_campaign(active_id)

        assert counter["n"] == 0
        assert client._campaign.id == active_id


def test_set_campaign_different_id_still_queries(apply_migrations, db_url):
    """Switching to a different campaign must still load it from the DB."""
    with RecapClient(url=db_url) as client:
        client.create_campaign("camp-a", "prop-a")
        id_a = client._campaign.id
        client.create_campaign("camp-b", "prop-b")  # B is now active

        # Active campaign is B; switching to A must run SQL.
        with count_statements(client) as counter:
            client.set_campaign(id_a)

        assert counter["n"] > 0
        assert client._campaign.id == id_a


def test_set_campaign_via_schema_short_circuits(apply_migrations, db_url):
    """Passing the active campaign as a schema also short-circuits."""
    with RecapClient(url=db_url) as client:
        client.create_campaign("schema-camp", "schema-prop")
        active = client._campaign

        with count_statements(client) as counter:
            client.set_campaign(campaign=active)

        assert counter["n"] == 0
        assert client._campaign.id == active.id


def test_set_campaign_force_reloads_issues_sql(apply_migrations, db_url):
    """``force=True`` must re-query the active campaign (escape hatch for
    out-of-band edits), even though the short-circuit would otherwise skip
    the round-trip."""
    with RecapClient(url=db_url) as client:
        client.create_campaign("force-camp", "force-prop")
        active_id = client._campaign.id

        with count_statements(client) as counter:
            client.set_campaign(active_id, force=True)

        assert counter["n"] > 0
        assert client._campaign.id == active_id
