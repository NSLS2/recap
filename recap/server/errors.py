"""Safe HTTP error contracts for the REST API."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from http import HTTPStatus
from typing import Any

from pydantic import BaseModel, ConfigDict
from starlette.responses import JSONResponse

from recap.exceptions import AuthorizationDenied  # noqa: F401


class ErrorCode(str, Enum):
    AUTHENTICATION_REQUIRED = "authentication_required"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    SERVICE_UNAVAILABLE = "service_unavailable"
    VALIDATION_ERROR = "validation_error"
    CONFLICT = "conflict"
    INTERNAL_ERROR = "internal_error"
    REQUEST_ERROR = "request_error"


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ErrorCode
    message: str
    request_id: str


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    error: ErrorDetail


def safe_error_response(
    *,
    status_code: int,
    code: ErrorCode,
    message: str,
    request_id: str,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    envelope = ErrorEnvelope(
        error=ErrorDetail(code=code, message=message, request_id=request_id)
    )
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json"),
        headers=dict(headers or {}),
    )


def safe_http_error(status_code: int) -> tuple[ErrorCode, str]:
    errors: dict[int, tuple[ErrorCode, str]] = {
        401: (ErrorCode.AUTHENTICATION_REQUIRED, "Authentication required"),
        403: (ErrorCode.PERMISSION_DENIED, "Permission denied"),
        404: (ErrorCode.NOT_FOUND, "Not found"),
    }
    if status_code in errors:
        return errors[status_code]
    try:
        message = HTTPStatus(status_code).phrase
    except ValueError:
        message = "Request failed"
    return ErrorCode.REQUEST_ERROR, message


def request_id_from(scope: Any) -> str:
    return str(scope.state.request_id)
