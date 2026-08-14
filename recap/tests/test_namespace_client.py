from uuid import uuid4

from recap.client import RecapClient
from recap.lifecycle import LifecycleStatus
from recap.schemas.namespace import NamespaceContext


def test_namespace_returns_scoped_client_with_shared_backend(tmp_path):
    with RecapClient.from_sqlite(tmp_path / "recap.db") as client:
        scoped = client.namespace("beamline/amx")

        assert isinstance(scoped, RecapClient)
        assert scoped.namespace_path == "beamline/amx"
        assert scoped.backend is client.backend

        scoped.close()


def test_scoped_client_uses_scope_for_queries_and_namespace_creation(tmp_path):
    with RecapClient.from_sqlite(tmp_path / "recap.db") as client:
        client.create_namespace("beamline")
        scoped = client.namespace("beamline/amx")
        context = scoped.create_namespace(scoped.namespace_path, {"beamline": "amx"})

        assert context.path == "beamline/amx"
        assert scoped.query_maker().resources()._context == context

        scoped.close()


def test_namespace_context_tracks_metadata_revision_and_local_etag(tmp_path):
    with RecapClient.from_sqlite(tmp_path / "recap.db") as client:
        context = client.create_namespace(
            "beamline", {"owner": "amx", "beamline": "x"}
        )

        assert context.metadata == {"owner": "amx", "beamline": "x"}
        assert context.revision == 1
        assert context.etag == '"1"'

        updated = client.update_namespace(
            metadata={"owner": "fmx"},
            status=LifecycleStatus.ACTIVE,
        )

        assert updated.metadata == {"owner": "fmx", "beamline": "x"}
        assert updated.revision == 2
        assert updated.etag == '"2"'
        assert client.namespace_context == updated


def test_remote_namespace_context_preserves_response_etag(monkeypatch):
    client = RecapClient.from_url("http://recap.test", api_key="secret")
    namespace_id = uuid4()

    captured_context = None

    def execute(command, context):
        nonlocal captured_context
        captured_context = context
        return NamespaceContext(
            id=namespace_id,
            path=command.path,
            metadata=command.metadata or {},
            status=LifecycleStatus.ACTIVE,
            revision=7,
            etag='W/"remote-7"',
        )

    monkeypatch.setattr(
        client.backend.writer,
        "execute",
        execute,
    )

    context = client.create_namespace("beamline", {"owner": "amx"})

    assert context.id == namespace_id
    assert context.metadata == {"owner": "amx"}
    assert context.revision == 7
    assert context.etag == 'W/"remote-7"'
    assert captured_context is not None
    client.close()


def test_remote_namespace_update_forwards_active_server_etag(monkeypatch):
    client = RecapClient.from_url("http://recap.test", api_key="secret")
    namespace_id = uuid4()
    client._namespace_context = NamespaceContext(
        id=namespace_id,
        path="beamline/amx",
        revision=7,
        etag='W/"server-7"',
    )
    captured = {}

    def execute(command, context, *, etag_override=None):
        captured["command"] = command
        captured["etag_override"] = etag_override
        return NamespaceContext(
            id=namespace_id,
            path="beamline/amx",
            revision=8,
            etag='W/"server-8"',
        )

    monkeypatch.setattr(client.backend.writer, "execute", execute)

    client.update_namespace(metadata={"owner": "fmx"})

    assert captured["command"].expected_revision == 7
    assert captured["etag_override"] == 'W/"server-7"'
    client.close()
