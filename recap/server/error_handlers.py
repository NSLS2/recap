from __future__ import annotations

from fastapi import FastAPI, Request

from recap.commands.errors import (
    CommandConflictError,
    CommandError,
    CommandNotFoundError,
    CommandValidationError,
)
from recap.server.errors import ErrorCode, request_id_from, safe_error_response


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
                "Request validation failed",
            )
        else:
            status_code, code, message = 400, ErrorCode.REQUEST_ERROR, "Request failed"
        return safe_error_response(
            status_code=status_code,
            code=code,
            message=message,
            request_id=request_id_from(request),
        )
