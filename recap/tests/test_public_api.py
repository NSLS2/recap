import recap
from recap import (
    Direction,
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


def test_namespace_client_facade_is_not_public():
    assert not hasattr(recap, "NamespaceClient")
