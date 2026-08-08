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
        scoped = client.namespace("name")
        assert scoped.namespace_path == "name"
        scoped.close()
        assert client.database_path == db_file


def test_query_maker_uses_client_namespace_scope(apply_migrations, db_path):
    with RecapClient.from_sqlite(db_path) as client:
        context = client.create_namespace("query-name")
        qm = client.namespace(context.path).query_maker()

        assert qm.process_runs()._context == context
        assert qm.resources()._context == context
        assert qm.process_templates()._context == context
        assert qm.process_runs()._spec.on_unloaded == "warn"


def test_query_maker_uses_scoped_namespace_view(apply_migrations, db_path):
    with RecapClient.from_sqlite(db_path) as client:
        default = client.create_namespace("client-default")
        other = client.create_namespace("client-other")
        qm = client.namespace(other.path).query_maker()

        assert qm.process_runs()._context == other
        assert qm.resources()._context == other


def test_query_maker_can_set_on_unloaded_policy(apply_migrations, db_path):
    with RecapClient.from_sqlite(db_path) as client:
        context = client.create_namespace("name-policy")
        qm = client.namespace(context.path).query_maker(on_unloaded="raise")
        assert qm.process_runs()._spec.on_unloaded == "raise"


def test_root_query_maker_uses_root_scope_remotely():
    client = RecapClient.from_url("http://recap.test", api_key="secret")

    query = client.query_maker().resources()

    assert query._context.path == ""
    client.close()


def test_scoped_permissions_use_client_namespace(monkeypatch):
    client = RecapClient.from_url("http://recap.test", api_key="secret")
    calls = []
    monkeypatch.setattr(
        client._read_backend,
        "permissions",
        lambda path: calls.append(path) or object(),
    )

    client.namespace("beamline/amx").permissions()

    assert calls == ["beamline/amx"]
    client.close()


def test_scoped_remote_query_uses_view_namespace():
    client = RecapClient.from_url("http://recap.test", api_key="secret")
    scoped = client.namespace("beamline/amx")

    query = scoped.query_maker().resources()

    assert query._context.path == "beamline/amx"
    scoped.close()
    client.close()


def test_builder_namespace_argument_is_rejected(tmp_path):
    with RecapClient.from_sqlite(tmp_path / "recap.db", namespace="beamline/amx") as client:
        with pytest.raises(TypeError):
            client.build_resource_template(
                name="Sample",
                type_names=["sample"],
                namespace_path="beamline/other",
            )
