from pathlib import Path
from tempfile import gettempdir

import pytest

from recap.client.base_client import RecapClient
from recap.schemas.namespace import NamespaceContext


def test_build_process_run_requires_namespace(client):
    client._namespace_context = None
    with pytest.raises(ValueError):
        client.build_process_run("run", "desc", "tmpl", "1.0")


def test_from_sqlite_returns_root_recap_client(tmp_path):
    client = RecapClient.from_sqlite(tmp_path / "recap.db")

    assert isinstance(client, RecapClient)
    assert client.namespace_path == ""
    client.close()


def test_factories_accept_initial_namespace_scope(tmp_path):
    local = RecapClient.from_sqlite(
        tmp_path / "recap.db", namespace="beamline/amx"
    )

    assert local.namespace_path == "beamline/amx"
    local.close()


def test_build_resource_template_validates_type_names(apply_migrations, db_path):
    with RecapClient.from_sqlite(db_path) as client:
        client.create_namespace("validation")
        with pytest.raises(TypeError):
            client.build_resource_template(name="Bad", type_names="not-a-list")

        with pytest.raises(TypeError):
            client.build_resource_template(name="Bad2", type_names=["ok", 123])


def test_from_sqlite_uses_temp_dir():
    with RecapClient.from_sqlite() as client:
        assert client.database_path is not None
        assert client.database_path.exists()
        assert client.database_path.parent == Path(gettempdir())
        client.create_namespace("name")
        assert isinstance(client.namespace_context, NamespaceContext)

    if client.database_path and client.database_path.exists():
        client.database_path.unlink()


def test_from_sqlite_reuses_existing_file(tmp_path):
    db_file = tmp_path / "recap.db"

    with RecapClient.from_sqlite(db_file) as client:
        client.create_namespace("name")
        existing_id = client.namespace_context.id

    with RecapClient.from_sqlite(db_file) as client:
        client.set_namespace(existing_id)
        assert client.namespace_context.id == existing_id
        assert client.database_path == db_file


def test_query_maker_uses_explicit_namespace_context(apply_migrations, db_path):
    with RecapClient.from_sqlite(db_path) as client:
        context = client.create_namespace("query-name")
        qm = client.query_maker(context=context)

        assert qm.process_runs()._context == context
        assert qm.resources()._context == context
        assert qm.process_templates()._context == context
        assert qm.process_runs()._spec.on_unloaded == "warn"


def test_query_maker_can_use_another_namespace(apply_migrations, db_path):
    with RecapClient.from_sqlite(db_path) as client:
        default = client.create_namespace("client-default")
        other = client.create_namespace("client-other")
        client.set_namespace(default.id)
        qm = client.query_maker(context=other)

        assert qm.process_runs()._context == other
        assert qm.resources()._context == other


def test_query_maker_can_set_on_unloaded_policy(apply_migrations, db_path):
    with RecapClient.from_sqlite(db_path) as client:
        context = client.create_namespace("name-policy")
        qm = client.query_maker(context=context, on_unloaded="raise")
        assert qm.process_runs()._spec.on_unloaded == "raise"
