from enum import Enum


class LifecycleStatus(str, Enum):
    """Monotonic lifecycle state for persisted RECAP entities."""

    MUTABLE = "MUTABLE"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


_ALLOWED = {
    LifecycleStatus.MUTABLE: {LifecycleStatus.ACTIVE, LifecycleStatus.ARCHIVED},
    LifecycleStatus.ACTIVE: {LifecycleStatus.ARCHIVED},
    LifecycleStatus.ARCHIVED: set(),
}


def validate_transition(source: LifecycleStatus, target: LifecycleStatus) -> None:
    """Validate lifecycle transition, allowing idempotent same-state updates.

    Parameters
    ----------
    source, target
        Current and requested lifecycle states.

    Raises
    ------
    ValueError
        If transition would move an entity backwards or out of ``ARCHIVED``.
    """
    if source == target:
        return
    if target not in _ALLOWED[source]:
        raise ValueError(f"Invalid lifecycle transition: {source} -> {target}")
