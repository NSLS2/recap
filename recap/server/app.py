"""FastAPI application factory for the recap GraphQL server."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import SecretStr
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from recap.authentication.api_key import ApiKeyRequestAuthenticator
from recap.authorization.snapshot import SnapshotUnavailable
from recap.client import RecapClient
from recap.server.errors import (
    AuthorizationDenied,
    ErrorCode,
    request_id_from,
    safe_error_response,
    safe_http_error,
)
from recap.server.security import authenticate_request
from recap.server.strawberry_schema import build_router


def create_app(
    db_path: str | Path, *, api_key: str | SecretStr | None = None
) -> FastAPI:
    """Create the recap FastAPI application.

    Args:
        db_path: Path to the SQLite database file. Created if it doesn't exist.
        api_key: Optional key that enables authentication on every route.

    Returns:
        Configured FastAPI application with /graphql and /db_path endpoints.
    """
    db_path = Path(db_path)
    client = RecapClient.from_sqlite(db_path)
    backend = client.backend
    graphql_router = build_router(backend)

    app = FastAPI(
        title="recap GraphQL server",
        description="Read-only GraphQL API for recap experiment provenance data.",
        version="1.0.0",
        dependencies=[Depends(authenticate_request)] if api_key is not None else None,
    )

    if api_key is not None:
        app.state.request_authenticator = ApiKeyRequestAuthenticator(api_key)

    @app.middleware("http")
    async def secure_request_context(request: Request, call_next) -> Response:
        request.state.request_id = str(uuid4())
        try:
            response = await call_next(request)
        except Exception:
            response = safe_error_response(
                status_code=500,
                code=ErrorCode.INTERNAL_ERROR,
                message="Internal server error",
                request_id=request_id_from(request),
            )
        response.headers["X-Request-ID"] = request_id_from(request)
        return response

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(
        request: Request, error: StarletteHTTPException
    ) -> Response:
        code, message = safe_http_error(error.status_code)
        return safe_error_response(
            status_code=error.status_code,
            code=code,
            message=message,
            request_id=request_id_from(request),
            headers=error.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, error: RequestValidationError
    ) -> Response:
        return safe_error_response(
            status_code=422,
            code=ErrorCode.VALIDATION_ERROR,
            message="Request validation failed",
            request_id=request_id_from(request),
        )

    @app.exception_handler(AuthorizationDenied)
    async def handle_authorization_denied(
        request: Request, error: AuthorizationDenied
    ) -> Response:
        status_code = 404 if error.conceal else 403
        code, message = safe_http_error(status_code)
        return safe_error_response(
            status_code=status_code,
            code=code,
            message=message,
            request_id=request_id_from(request),
        )

    @app.exception_handler(SnapshotUnavailable)
    async def handle_snapshot_unavailable(
        request: Request, error: SnapshotUnavailable
    ) -> Response:
        return safe_error_response(
            status_code=503,
            code=ErrorCode.SERVICE_UNAVAILABLE,
            message="Service unavailable",
            request_id=request_id_from(request),
        )

    app.include_router(graphql_router, prefix="/graphql")

    @app.get("/db_path", summary="Get database path")
    def get_db_path() -> dict[str, str]:
        """Return the path to the SQLite database file used by this server.

        Used by RecapClient.from_url() to wire direct SQLite writes in Phase 1.
        Requires shared filesystem between client and server.
        """
        return {"db_path": str(db_path.resolve())}

    return app
