from pathlib import Path
from tempfile import gettempdir

import pytest

from recap.client.base_client import RecapClient
from recap.schemas.process import CampaignSchema


def test_build_process_run_requires_campaign(client):
    with pytest.raises(ValueError):
        client.build_process_run("run", "desc", "tmpl", "1.0")


def test_build_resource_template_validates_type_names(apply_migrations, db_url):
    with RecapClient(url=db_url) as client:
        with pytest.raises(TypeError):
            client.build_resource_template("Bad", "not-a-list")

        with pytest.raises(TypeError):
            client.build_resource_template("Bad2", ["ok", 123])


def test_from_sqlite_uses_temp_dir():
    with RecapClient.from_sqlite() as client:
        assert client.database_path is not None
        assert client.database_path.exists()
        assert client.database_path.parent == Path(gettempdir())
        client.create_campaign("name", "proposal")
        assert isinstance(client._campaign, CampaignSchema)

    if client.database_path and client.database_path.exists():
        client.database_path.unlink()


def test_from_sqlite_reuses_existing_file(tmp_path):
    db_file = tmp_path / "recap.db"

    with RecapClient.from_sqlite(db_file) as client:
        client.create_campaign("name", "proposal")
        existing_id = client._campaign.id

    with RecapClient.from_sqlite(db_file) as client:
        client.set_campaign(existing_id)
        assert client._campaign.id == existing_id
        assert client.database_path == db_file
