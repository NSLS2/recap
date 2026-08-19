from pathlib import Path
from tempfile import gettempdir
from uuid import UUID

import pytest

from recap.client.backend import ClientBackend
from recap.client.base_client import RecapClient
from recap.commands.models import CreateNamespace, UpdateNamespace
from recap.dsl.query import QuerySpec
from recap.lifecycle import LifecycleStatus
from recap.schemas.namespace import NamespaceContext
from recap.schemas.resource import ResourceRef, ResourceSchema


class _Reader:
    def __init__(self, result=None):
        self.result = result or []

    def query(self, schema, spec, *, namespace_path):
        return self.result

    def count(self, schema, spec, *, namespace_path):
        return len(self.result)


class _Writer:
    def __init__(self, result=None):
        self.commands = []
        self.result = result

    def execute(self, command, context, *, etag_override=None):
        self.commands.append((command, context))
        return self.result


class _Namespaces:
    def list_child_namespaces(self, parent_path):
        return [parent_path]


class _NamespaceWriter:
    def create_namespace(self, path, metadata, context):
        return (path, metadata, context)

    def update_namespace(
        self, namespace_id, expected_revision, metadata, status, context, *, etag=None
    ):
        return (namespace_id, expected_revision, metadata, status, context, etag)


class _ClosableFake:
    def __init__(self):
        self.close_count = 0

    def query(self, schema, spec, *, namespace_path):
        return []

    def count(self, schema, spec, *, namespace_path):
        return 0

    def execute(self, command, context):
        return None

    def list_child_namespaces(self, parent_path):
        return []

    def create_namespace(self, path, metadata, context):
        return None

    def update_namespace(
        self, namespace_id, expected_revision, metadata, status, context, *, etag=None
    ):
        return None

    def close(self):
        self.close_count += 1


def _backend_values():
    return {
        "reader": _Reader(),
        "writer": _Writer(),
        "namespaces": _Namespaces(),
        "namespace_writer": _NamespaceWriter(),
    }


def test_client_backend_accepts_distinct_capability_objects():
    values = _backend_values()
    backend = ClientBackend(**values)

    assert backend.reader is values["reader"]
    assert backend.writer is values["writer"]
    assert backend.namespaces is values["namespaces"]
    assert backend.namespace_writer is values["namespace_writer"]


def test_client_backend_delegates_query_count_and_execute():
    reader = _Reader()
    reader.result = ["read"]
    writer = _Writer("written")
    backend = ClientBackend(
        reader=reader,
        writer=writer,
        namespaces=_Namespaces(),
        namespace_writer=_NamespaceWriter(),
    )
    spec = QuerySpec()

    assert backend.query(ResourceSchema, spec, namespace_path="scope") == ["read"]
    assert backend.count(ResourceSchema, spec, namespace_path="scope") == 1
    assert backend._execute("command", "context") == "written"


def test_client_backend_delegates_namespace_operations():
    namespaces = _Namespaces()
    namespace_writer = _NamespaceWriter()
    backend = ClientBackend(
        reader=_Reader(),
        writer=_Writer(),
        namespaces=namespaces,
        namespace_writer=namespace_writer,
    )

    assert backend.list_child_namespaces("scope") == ["scope"]
    assert backend.create_namespace("scope", {}, "context") == ("scope", {}, "context")
    assert backend.update_namespace("id", 1, {}, None, "context", etag="etag") == (
        "id",
        1,
        {},
        None,
        "context",
        "etag",
    )


def test_client_backend_closes_shared_capability_once():
    shared = _ClosableFake()
    backend = ClientBackend(
        reader=shared,
        writer=shared,
        namespaces=shared,
        namespace_writer=shared,
    )

    backend.close()

    assert shared.close_count == 1


def test_client_close_delegates_to_backend():
    shared = _ClosableFake()
    backend = ClientBackend(
        reader=shared,
        writer=shared,
        namespaces=shared,
        namespace_writer=shared,
    )
    client = RecapClient._from_backends(backend)

    client.close()

    assert shared.close_count == 1

def test_client_backend_is_frozen():
    backend = ClientBackend(**_backend_values())

    with pytest.raises(AttributeError):
        backend.reader = _Reader()


@pytest.mark.parametrize("field", ["reader", "writer", "namespaces", "namespace_writer"])
def test_client_backend_rejects_missing_required_capability(field):
    values = _backend_values()
    values[field] = object()

    with pytest.raises(TypeError, match=field):
        ClientBackend(**values)


def test_client_stores_typed_reader_and_writer():
    reader = _Reader()
    writer = _Writer()
    backend = ClientBackend(
        reader=reader,
        writer=writer,
        namespaces=_Namespaces(),
        namespace_writer=_NamespaceWriter(),
    )
    client = RecapClient._from_backends(backend)

    assert client.backend is backend
    assert client.backend.reader is reader
    assert client.backend.writer is writer
    client.close()


def test_copy_resource_routes_through_write_capability():
    expected = ResourceSchema.model_construct(id=UUID(int=1), name="copy")
    reader = _Reader()
    writer = _Writer(expected)
    backend = ClientBackend(
        reader=reader,
        writer=writer,
        namespaces=_Namespaces(),
        namespace_writer=_NamespaceWriter(),
    )
    client = RecapClient._from_backends(backend, namespace="scope")

    assert client.copy_resource(UUID(int=2)) is expected
    assert writer.commands[0][0].source_resource_id == UUID(int=2)
    client.close()


def test_namespace_operations_route_through_command_writer():
    context = NamespaceContext(
        id=UUID(int=3),
        path="scope",
        metadata={},
        status=LifecycleStatus.ACTIVE,
        revision=1,
    )

    class NamespaceWriter(_Writer):
        def execute(self, command, context, *, etag_override=None):
            self.commands.append((command, context))
            return self.context

    writer = NamespaceWriter()
    writer.context = context
    namespace_writer = _NamespaceWriter()
    backend = ClientBackend(
        reader=_Reader(),
        writer=writer,
        namespaces=_Namespaces(),
        namespace_writer=namespace_writer,
    )
    client = RecapClient._from_backends(backend)

    assert client.create_namespace("scope") == context
    assert client.update_namespace() == context
    assert isinstance(writer.commands[0][0], CreateNamespace)
    assert isinstance(writer.commands[1][0], UpdateNamespace)
    client.close()


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


def test_blank_database_copies_are_isolated(blank_database_path, copy_database, tmp_path):
    first_path = copy_database(blank_database_path, tmp_path / "first.db")
    second_path = copy_database(blank_database_path, tmp_path / "second.db")

    with RecapClient.from_sqlite(first_path) as first:
        first.create_namespace("only-first")

    with RecapClient.from_sqlite(second_path) as second:
        assert second.backend.namespaces.list_child_namespace_paths("") == []


def test_client_fixture_starts_at_root_scope(client):
    assert client.namespace_path == ""
    assert client.backend.namespaces.list_child_namespace_paths("") == []


def test_query_maker_uses_client_namespace_scope(apply_migrations, db_path):
    with RecapClient.from_sqlite(db_path) as client:
        context = client.create_namespace("query-name")
        qm = client.namespace(context.path).query_maker()

        assert qm.process_runs()._context == context
        assert qm.resources()._context == context
        assert qm.process_templates()._context == context
        assert qm.process_runs()._spec.on_unloaded == "warn"


def test_query_maker_receives_client_backend_reader_facade(client):
    query = client.query_maker()

    assert query.backend is client.backend


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


def test_root_query_maker_uses_root_scope_remotely(monkeypatch):
    from recap.adapter.rest import RESTAdapter

    client = RecapClient.from_url("http://recap.test", api_key="secret")
    monkeypatch.setattr(
        RESTAdapter,
        "get_namespace_context",
        lambda _adapter, path: NamespaceContext(id=UUID(int=0), path=path, metadata={}),
    )

    query = client.query_maker().resources()

    assert query._context.path == ""
    client.close()


def test_scoped_permissions_use_client_namespace(monkeypatch):
    client = RecapClient.from_url("http://recap.test", api_key="secret")
    calls = []
    monkeypatch.setattr(
        client.backend.reader,
        "permissions",
        lambda path: calls.append(path) or object(),
    )

    client.namespace("beamline/amx").permissions()

    assert calls == ["beamline/amx"]
    client.close()


def test_remote_get_resource_uses_read_backend(monkeypatch):
    from recap.adapter.rest import RESTAdapter

    client = RecapClient.from_url("http://recap.test", api_key="secret")
    expected = ResourceRef.model_construct(id=UUID(int=1), name="sample")
    calls = []

    def read(*args, **kwargs):
        calls.append((args, kwargs))
        return [expected]

    monkeypatch.setattr(client.backend.reader, "query", read)
    monkeypatch.setattr(
        RESTAdapter,
        "get_namespace_context",
        lambda _adapter, path: NamespaceContext(id=UUID(int=0), path=path, metadata={}),
    )
    monkeypatch.setattr(
        client.backend.writer,
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

    monkeypatch.setattr(client.backend.reader, "query", read)

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
    monkeypatch.setattr(client.backend.reader, "query", lambda *args, **kwargs: matches)

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

    monkeypatch.setattr(client.backend.reader, "query", read)

    assert client._resolve_parent(expected.id, NamespaceContext(
        id=UUID(int=3), path="beamline", metadata={},
        status=LifecycleStatus.ACTIVE, revision=1
    )) is expected
    assert calls[0][1]["namespace_path"] == "beamline"
    client.close()


def test_scoped_remote_query_uses_view_namespace(monkeypatch):
    from recap.adapter.rest import RESTAdapter

    client = RecapClient.from_url("http://recap.test", api_key="secret")
    scoped = client.namespace("beamline/amx")
    monkeypatch.setattr(
        RESTAdapter,
        "get_namespace_context",
        lambda _adapter, path: NamespaceContext(id=UUID(int=0), path=path, metadata={}),
    )

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
