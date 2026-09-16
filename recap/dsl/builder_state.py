from typing import Any

from pydantic import BaseModel, Field

from recap.lifecycle import LifecycleStatus


class BuilderChanges(BaseModel):
    fields: dict[str, Any] = Field(default_factory=dict)
    lifecycle: LifecycleStatus | None = None


class BuilderTransactionState:
    def __init__(self) -> None:
        self._depth = 0
        self._pending_lifecycle: LifecycleStatus | None = None
        self._owned_lifecycle: dict[object, LifecycleStatus] = {}
        self._failed = False

    @property
    def in_context(self) -> bool:
        return self._depth > 0

    @property
    def pending_lifecycle(self) -> LifecycleStatus | None:
        return self._pending_lifecycle

    def pending_lifecycle_for(self, owner: object) -> LifecycleStatus | None:
        return self._owned_lifecycle.get(owner)

    def enter(self) -> None:
        if self._depth == 0:
            self._failed = False
        self._depth += 1

    def exit(self, exc_type: type[BaseException] | None) -> bool:
        if exc_type is not None:
            self._failed = True
        self._depth -= 1
        if self._depth < 0:
            raise RuntimeError("Builder context depth underflow")
        return self._depth == 0 and not self._failed

    def request_lifecycle(
        self, status: LifecycleStatus, *, owner: object | None = None
    ) -> None:
        if owner is not None:
            pending = self._owned_lifecycle.get(owner)
            if pending not in (None, status):
                raise ValueError("Conflicting lifecycle requests")
            self._owned_lifecycle[owner] = status
            return
        if self._pending_lifecycle not in (None, status):
            raise ValueError("Conflicting lifecycle requests")
        self._pending_lifecycle = status

    def clear_lifecycle(self, *, owner: object | None = None) -> None:
        if owner is not None:
            self._owned_lifecycle.pop(owner, None)
            return
        self._pending_lifecycle = None
