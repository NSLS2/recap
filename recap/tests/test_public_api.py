import recap
from recap import (
    Direction,
    ExportContext,
    Exporter,
    ExporterRegistry,
    Field,
    LifecycleStatus,
    QueryDSL,
    RecapClient,
    validate_transition,
)


def test_core_public_exports_are_importable():
    assert RecapClient is not None
    assert QueryDSL is not None
    assert Field is not None
    assert Direction.input.value == "input"
    assert LifecycleStatus.MUTABLE.value == "MUTABLE"
    assert callable(validate_transition)
    assert ExportContext is not None
    assert Exporter is not None
    assert ExporterRegistry is not None


def test_namespace_client_facade_is_not_public():
    assert not hasattr(recap, "NamespaceClient")


def test_all_declared_public_exports_resolve():
    assert len(recap.__all__) == len(set(recap.__all__))
    assert all(getattr(recap, name) is not None for name in recap.__all__)
