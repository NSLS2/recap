from recap.client import RecapClient


def test_namespace_returns_scoped_client_with_shared_backend(tmp_path):
    with RecapClient.from_sqlite(tmp_path / "recap.db") as client:
        scoped = client.namespace("beamline/amx")

        assert isinstance(scoped, RecapClient)
        assert scoped.namespace_path == "beamline/amx"
        assert scoped.backend is client.backend

        scoped.close()


def test_scoped_client_uses_scope_for_queries_and_namespace_creation(tmp_path):
    with RecapClient.from_sqlite(tmp_path / "recap.db") as client:
        scoped = client.namespace("beamline/amx")
        context = scoped.create_namespace(scoped.namespace_path, {"beamline": "amx"})

        assert context.path == "beamline/amx"
        assert scoped.query_maker().resources()._context == context

        scoped.close()
