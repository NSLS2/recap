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

    @property
    def in_context(self) -> bool:
        return self._depth > 0

    @property
    def pending_lifecycle(self) -> LifecycleStatus | None:
        return self._pending_lifecycle

    def enter(self) -> None:
        self._depth += 1

    def exit(self, exc_type: type[BaseException] | None) -> bool:
        self._depth -= 1
        if self._depth < 0:
            raise RuntimeError("Builder context depth underflow")
        return self._depth == 0 and exc_type is None

    def request_lifecycle(self, status: LifecycleStatus) -> None:
        if self._pending_lifecycle not in (None, status):
            raise ValueError("Conflicting lifecycle requests")
        self._pending_lifecycle = status

    def clear_lifecycle(self) -> None:
        self._pending_lifecycle = None
