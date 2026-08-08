import recap
from recap import Direction, Field, LifecycleStatus, QueryDSL, RecapClient, validate_transition


def test_core_public_exports_are_importable():
    assert RecapClient is not None
    assert QueryDSL is not None
    assert Field is not None
    assert Direction.input.value == "input"
    assert LifecycleStatus.MUTABLE.value == "MUTABLE"
    assert callable(validate_transition)


def test_namespace_is_scoped_recap_client_and_facade_is_not_public(tmp_path):
    assert not hasattr(recap, "NamespaceClient")

    with RecapClient.from_sqlite(tmp_path / "recap.db") as client:
        scoped = client.namespace("beamline/amx")
        assert isinstance(scoped, RecapClient)
        assert scoped.namespace_path == "beamline/amx"
