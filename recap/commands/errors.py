from __future__ import annotations

from typing import ClassVar


class CommandError(RuntimeError):
    """Base class for safe, transport-independent command failures."""

    code: ClassVar[str]


class CommandNotFoundError(CommandError):
    code = "not_found"


class CommandConflictError(CommandError):
    code = "conflict"


class CommandValidationError(CommandError):
    code = "validation_error"

    def __init__(self, message: str, *, public_message: str | None = None) -> None:
        super().__init__(message)
        self.public_message = public_message
