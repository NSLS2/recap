import pytest

from recap.client.base_client import RecapClient


def test_build_process_run_requires_campaign(client):
    with pytest.raises(ValueError):
        client.build_process_run("run", "desc", "tmpl", "1.0")


def test_build_resource_template_validates_type_names(db_url, setup_database):
    with RecapClient(url=db_url) as client:
        with pytest.raises(TypeError):
            client.build_resource_template("Bad", "not-a-list")

        with pytest.raises(TypeError):
            client.build_resource_template("Bad2", ["ok", 123])
