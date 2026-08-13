from pathlib import Path
from tempfile import gettempdir
from uuid import UUID

import pytest

from recap.client.base_client import RecapClient
from recap.schemas.namespace import NamespaceContext
from recap.schemas.resource import ResourceRef
from recap.lifecycle import LifecycleStatus


def test_build_process_run_resolves_root_namespace(tmp_path):
    with RecapClient.from_sqlite(tmp_path / "recap.db") as client:
        client._namespace_context = None
        with client.build_process_template("tmpl", "1.0") as template:
            template.get_model()

        with client.build_process_run("run", "desc", "tmpl", "1.0") as builder:
            assert builder.namespace_path == ""


def test_from_sqlite_returns_root_recap_client(tmp_path):
    client = RecapClient.from_sqlite(tmp_path / "recap.db")

    assert isinstance(client, RecapClient)
    assert client.namespace_path == ""
    client.close()


def test_factories_accept_initial_namespace_scope(tmp_path):
    local = RecapClient.from_sqlite(tmp_path / "recap.db", namespace="beamline/amx")

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


def test_remote_get_resource_uses_read_backend(monkeypatch):
    client = RecapClient.from_url("http://recap.test", api_key="secret")
    expected = ResourceRef.model_construct(id=UUID(int=1), name="sample")
    calls = []

    def read(*args, **kwargs):
        calls.append((args, kwargs))
        return [expected]

    monkeypatch.setattr(client._read_backend, "query", read)
    monkeypatch.setattr(
        client.backend,
        "get_resource",
        lambda *args, **kwargs: pytest.fail("remote read used REST backend"),
        raising=False,
    )

    assert client.get_resource("sample", "Sample") is expected
    assert calls[0][1]["namespace_path"] == ""
    client.close()


def test_get_resource_uses_supported_relationship_filters(monkeypatch, tmp_path):
    client = RecapClient.from_sqlite(tmp_path / "recap.db")
    expected = ResourceRef.model_construct(id=UUID(int=4), name="sample")
    calls = []

    def read(*args, **kwargs):
        calls.append((args, kwargs))
        return [expected]

    monkeypatch.setattr(client._read_backend, "query", read)

    assert client.get_resource("sample", "Sample", "2.0") is expected
    assert calls[0][0][1].filters == {
        "name": "sample",
        "template__name": "Sample",
        "template__version": "2.0",
    }
    client.close()


def test_get_resource_rejects_multiple_matches(monkeypatch, tmp_path):
    client = RecapClient.from_sqlite(tmp_path / "recap.db")
    matches = [
        ResourceRef.model_construct(id=UUID(int=5), name="sample"),
        ResourceRef.model_construct(id=UUID(int=6), name="sample"),
    ]
    monkeypatch.setattr(client._read_backend, "query", lambda *args, **kwargs: matches)

    with pytest.raises(ValueError, match="[Mm]ultiple resources"):
        client.get_resource("sample", "Sample")
    client.close()


def test_uuid_parent_resolution_uses_read_backend(monkeypatch):
    client = RecapClient.from_url("http://recap.test", api_key="secret")
    expected = ResourceRef.model_construct(id=UUID(int=2), name="parent")
    calls = []

    def read(*args, **kwargs):
        calls.append((args, kwargs))
        return [expected]

    monkeypatch.setattr(client._read_backend, "query", read)
    monkeypatch.setattr(
        client.backend,
        "query",
        lambda *args, **kwargs: pytest.fail("parent lookup used write backend"),
        raising=False,
    )

    assert client._resolve_parent(expected.id, NamespaceContext(
        id=UUID(int=3), path="beamline", metadata={},
        status=LifecycleStatus.ACTIVE, revision=1
    )) is expected
    assert calls[0][1]["namespace_path"] == "beamline"
    client.close()


def test_scoped_remote_query_uses_view_namespace():
    client = RecapClient.from_url("http://recap.test", api_key="secret")
    scoped = client.namespace("beamline/amx")

    query = scoped.query_maker().resources()

    assert query._context.path == "beamline/amx"
    scoped.close()
    client.close()


def test_builder_namespace_argument_is_rejected(tmp_path):
    with (RecapClient.from_sqlite(
        tmp_path / "recap.db", namespace="beamline/amx"
    ) as client, pytest.raises(TypeError)):
            client.build_resource_template(
                name="Sample",
                type_names=["sample"],
                namespace_path="beamline/other",
            )
