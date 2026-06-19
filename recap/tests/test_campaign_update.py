"""Tests for campaign accessors and ``RecapClient.update_campaign``.

See ``RECAP_QUERY_OPTIMIZATION.md``: ``create_campaign``/``set_campaign`` now
return the active :class:`CampaignSchema`, a public ``campaign`` read accessor
is exposed, and a new ``update_campaign`` write path persists edits (via field
kwargs or an explicit schema) while keeping the client cache consistent.
"""

import pytest

from recap.client.base_client import RecapClient
from recap.schemas.process import CampaignSchema


def test_create_campaign_returns_schema(apply_migrations, db_url):
    with RecapClient(url=db_url) as client:
        result = client.create_campaign("ret-create", "ret-prop")
        assert isinstance(result, CampaignSchema)
        assert result.name == "ret-create"
        assert result.id == client._campaign.id


def test_create_campaign_persists_metadata(apply_migrations, db_url):
    """Regression: ``create_campaign(metadata=...)`` must persist to the
    ``meta_data`` column, not be silently dropped on the ORM instance."""
    with RecapClient(url=db_url) as client:
        created = client.create_campaign(
            "meta-camp", "meta-prop", metadata={"target": "mpro"}
        )
        cid = created.id

        # In-process return value carries the metadata.
        assert created.meta_data == {"target": "mpro"}

        # And it survives a real DB round-trip.
        reloaded = client.set_campaign(cid, force=True)
        assert reloaded.meta_data == {"target": "mpro"}


def test_set_campaign_returns_schema(apply_migrations, db_url):
    with RecapClient(url=db_url) as client:
        client.create_campaign("set-a", "set-prop-a")
        id_a = client._campaign.id
        client.create_campaign("set-b", "set-prop-b")

        result = client.set_campaign(id_a)
        assert isinstance(result, CampaignSchema)
        assert result.id == id_a


def test_campaign_property_returns_active(apply_migrations, db_url):
    with RecapClient(url=db_url) as client:
        assert client.campaign is None
        client.create_campaign("prop-camp", "prop-prop")
        assert client.campaign is not None
        assert client.campaign.id == client._campaign.id


def test_update_campaign_via_kwargs_persists(apply_migrations, db_url):
    with RecapClient(url=db_url) as client:
        client.create_campaign("upd-kwargs", "upd-kwargs-prop")
        cid = client._campaign.id

        client.update_campaign(name="upd-kwargs-new", saf="SAF-99")

        # Re-read from the DB to confirm persistence.
        reloaded = client.set_campaign(cid, force=True)
        assert reloaded.name == "upd-kwargs-new"
        assert reloaded.saf == "SAF-99"


def test_update_campaign_via_schema_persists(apply_migrations, db_url):
    with RecapClient(url=db_url) as client:
        client.create_campaign("upd-schema", "upd-schema-prop")
        cid = client._campaign.id

        edited = client.campaign.model_copy(update={"name": "upd-schema-new"})
        client.update_campaign(edited)

        reloaded = client.set_campaign(cid, force=True)
        assert reloaded.name == "upd-schema-new"


def test_update_campaign_updates_cache(apply_migrations, db_url):
    with RecapClient(url=db_url) as client:
        client.create_campaign("upd-cache", "upd-cache-prop")

        client.update_campaign(name="upd-cache-new")

        assert client.campaign.name == "upd-cache-new"


def test_update_campaign_no_active_raises(apply_migrations, db_url):
    with RecapClient(url=db_url) as client, pytest.raises(ValueError):
        client.update_campaign(name="nope")


def test_update_campaign_unknown_kwarg_raises(apply_migrations, db_url):
    with RecapClient(url=db_url) as client:
        client.create_campaign("upd-bad", "upd-bad-prop")
        with pytest.raises(TypeError):
            client.update_campaign(bogus="x")
