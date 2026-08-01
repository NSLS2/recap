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
