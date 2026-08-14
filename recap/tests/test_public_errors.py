import pytest

import recap
from recap.exceptions import (
    RecapAuthenticationError,
    RecapConflictError,
    RecapError,
    RecapInternalError,
    RecapNotFoundError,
    RecapPermissionDeniedError,
    RecapRequestError,
    RecapServiceUnavailableError,
    RecapValidationError,
    error_from_code,
)


@pytest.mark.parametrize(
    ("code", "error_type"),
    [
        ("authentication_required", RecapAuthenticationError),
        ("permission_denied", RecapPermissionDeniedError),
        ("not_found", RecapNotFoundError),
        ("validation_error", RecapValidationError),
        ("conflict", RecapConflictError),
        ("service_unavailable", RecapServiceUnavailableError),
        ("internal_error", RecapInternalError),
        ("request_error", RecapRequestError),
    ],
)
def test_error_from_code_builds_public_category(code, error_type):
    error = error_from_code(
        code,
        "Safe message",
        url="https://recap.example/api",
        status_code=409,
        request_id="request-7",
    )

    assert isinstance(error, error_type)
    assert isinstance(error, RecapError)
    assert error.code == code
    assert error.message == "Safe message"
    assert error.url == "https://recap.example/api"
    assert error.status_code == 409
    assert error.request_id == "request-7"
    assert str(error) == "Safe message; HTTP 409; request_id=request-7"


def test_unknown_code_maps_to_request_error():
    error = error_from_code("future_code", "Safe message")

    assert isinstance(error, RecapRequestError)
    assert error.code == "request_error"


def test_public_error_is_top_level_export():
    assert recap.RecapError is RecapError
