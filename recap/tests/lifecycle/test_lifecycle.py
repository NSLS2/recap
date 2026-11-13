import pytest

from recap.lifecycle import LifecycleStatus, validate_transition


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (LifecycleStatus.MUTABLE, LifecycleStatus.ACTIVE),
        (LifecycleStatus.MUTABLE, LifecycleStatus.ARCHIVED),
        (LifecycleStatus.ACTIVE, LifecycleStatus.ARCHIVED),
    ],
)
def test_allowed_lifecycle_transitions(source, target):
    validate_transition(source, target)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (LifecycleStatus.ACTIVE, LifecycleStatus.MUTABLE),
        (LifecycleStatus.ARCHIVED, LifecycleStatus.ACTIVE),
        (LifecycleStatus.ARCHIVED, LifecycleStatus.MUTABLE),
    ],
)
def test_rejects_reverse_lifecycle_transitions(source, target):
    with pytest.raises(ValueError, match="Invalid lifecycle transition"):
        validate_transition(source, target)
