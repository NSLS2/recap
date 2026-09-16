from dataclasses import FrozenInstanceError, fields

import pytest

from recap.commands.errors import (
    CommandConflictError,
    CommandError,
    CommandNotFoundError,
    CommandValidationError,
)
from recap.commands.models import CommandContext
from recap.exceptions import (
    RecapConflictError,
    RecapNotFoundError,
    RecapValidationError,
)


@pytest.mark.parametrize(
    ("error_type", "code", "public_type"),
    [
        (CommandNotFoundError, "not_found", RecapNotFoundError),
        (CommandConflictError, "conflict", RecapConflictError),
        (CommandValidationError, "validation_error", RecapValidationError),
    ],
)
def test_command_errors_have_stable_semantics(error_type, code, public_type):
    error = error_type("safe message")

    assert isinstance(error, CommandError)
    assert isinstance(error, public_type)
    assert error.code == code
    assert str(error) == "safe message"


def test_validation_error_supports_optional_public_message():
    default = CommandValidationError("internal message")
    explicit = CommandValidationError(
        "internal message", public_message="safe public message"
    )

    assert default.public_message is None
    assert explicit.public_message == "safe public message"
    assert str(explicit) == "internal message"


def test_command_context_has_exact_request_scoped_fields():
    assert [field.name for field in fields(CommandContext)] == [
        "actor",
        "request_id",
        "policy",
        "audit_sink",
        "authorization_generation",
        "idempotency_key",
    ]


def test_command_context_is_frozen():
    context = CommandContext(
        actor=object(),
        request_id="request-1",
        policy=object(),
        audit_sink=object(),
        authorization_generation=None,
    )

    with pytest.raises(FrozenInstanceError):
        context.request_id = "request-2"
