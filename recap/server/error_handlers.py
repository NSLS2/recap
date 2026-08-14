from __future__ import annotations

from fastapi import FastAPI, Request

from recap.commands.errors import (
    CommandConflictError,
    CommandError,
    CommandNotFoundError,
    CommandValidationError,
)
from recap.exceptions import RecapRequestError
from recap.server.errors import (
    AuthorizationDenied,
    ErrorCode,
    request_id_from,
    safe_error_response,
    safe_http_error,
)


def register_command_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(CommandError)
    async def handle_command_error(request: Request, error: CommandError):
        if isinstance(error, CommandNotFoundError):
            status_code, code, message = 404, ErrorCode.NOT_FOUND, "Not found"
        elif isinstance(error, CommandConflictError):
            status_code, code, message = 409, ErrorCode.CONFLICT, "Conflict"
        elif isinstance(error, CommandValidationError):
            status_code, code, message = (
                422,
                ErrorCode.VALIDATION_ERROR,
                error.public_message or "Request validation failed",
            )
        else:
            status_code, code, message = 400, ErrorCode.REQUEST_ERROR, "Request failed"
        return safe_error_response(
            status_code=status_code,
            code=code,
            message=message,
            request_id=request_id_from(request),
        )

    @app.exception_handler(RecapRequestError)
    async def handle_public_error(request: Request, error: RecapRequestError):
        public_errors = {
            ErrorCode.VALIDATION_ERROR: (
                422,
                ErrorCode.VALIDATION_ERROR,
                "Request validation failed",
            ),
            ErrorCode.CONFLICT: (409, ErrorCode.CONFLICT, "Conflict"),
            ErrorCode.INTERNAL_ERROR: (
                500,
                ErrorCode.INTERNAL_ERROR,
                "Internal server error",
            ),
        }
        status_code = {
            ErrorCode.AUTHENTICATION_REQUIRED: 401,
            ErrorCode.PERMISSION_DENIED: 403,
            ErrorCode.NOT_FOUND: 404,
            ErrorCode.SERVICE_UNAVAILABLE: 503,
            ErrorCode.VALIDATION_ERROR: 422,
            ErrorCode.CONFLICT: 409,
            ErrorCode.INTERNAL_ERROR: 500,
        }.get(error.code, 400)
        concealed = isinstance(error, AuthorizationDenied) and error.conceal
        if concealed:
            status_code = 404
        if concealed:
            code, message = safe_http_error(status_code)
        elif error.code in public_errors:
            _, code, message = public_errors[error.code]
        elif status_code == 503:
            code, message = ErrorCode.SERVICE_UNAVAILABLE, "Service unavailable"
        else:
            code, message = safe_http_error(status_code)
        return safe_error_response(
            status_code=status_code,
            code=code,
            message=message,
            request_id=request_id_from(request),
        )
