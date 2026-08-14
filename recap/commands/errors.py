from __future__ import annotations

from typing import ClassVar

from recap.exceptions import (
    RecapConflictError,
    RecapNotFoundError,
    RecapRequestError,
    RecapValidationError,
)


class CommandError(RecapRequestError):
    """Base class for safe, transport-independent command failures."""

    code: ClassVar[str]


class CommandNotFoundError(RecapNotFoundError, CommandError):
    code = "not_found"


class CommandConflictError(RecapConflictError, CommandError):
    code = "conflict"


class CommandValidationError(RecapValidationError, CommandError):
    code = "validation_error"

    def __init__(self, message: str, *, public_message: str | None = None) -> None:
        super().__init__(message)
        self.public_message = public_message
