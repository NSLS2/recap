from datetime import UTC, datetime
from uuid import uuid4

from recap.adapter.rest import RESTResult
from recap.client import RecapClient
from recap.lifecycle import LifecycleStatus


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
    monkeypatch.setattr(
        client.backend,
        "create_namespace",
        lambda path, metadata: RESTResult(
            entity={
                "id": str(namespace_id),
                "path": path,
                "metadata": metadata,
                "status": "ACTIVE",
                "revision": 7,
                "create_date": datetime.now(UTC),
                "modified_date": datetime.now(UTC),
            },
            etag='W/"remote-7"',
            request_id="request-1",
        ),
    )

    context = client.create_namespace("beamline", {"owner": "amx"})

    assert context.id == namespace_id
    assert context.metadata == {"owner": "amx"}
    assert context.revision == 7
    assert context.etag == 'W/"remote-7"'
    client.close()
