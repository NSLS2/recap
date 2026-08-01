from enum import Enum


class LifecycleStatus(str, Enum):
    MUTABLE = "MUTABLE"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


_ALLOWED = {
    LifecycleStatus.MUTABLE: {LifecycleStatus.ACTIVE, LifecycleStatus.ARCHIVED},
    LifecycleStatus.ACTIVE: {LifecycleStatus.ARCHIVED},
    LifecycleStatus.ARCHIVED: set(),
}


def validate_transition(source: LifecycleStatus, target: LifecycleStatus) -> None:
    if source == target:
        return
    if target not in _ALLOWED[source]:
        raise ValueError(f"Invalid lifecycle transition: {source} -> {target}")
