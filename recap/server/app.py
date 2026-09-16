"""FastAPI application factory for the recap REST server."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import SecretStr
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from recap.authentication.api_key import ApiKeyRequestAuthenticator
from recap.db.engine import create_engine_and_session_factory
from recap.server.error_handlers import register_command_error_handlers
from recap.server.errors import (
    ErrorCode,
    request_id_from,
    safe_error_response,
    safe_http_error,
)
from recap.server.rest import router as rest_router
from recap.server.security import authenticate_request
from recap.utils.migrations import apply_migrations


@dataclass(frozen=True, slots=True)
class _DatabaseConfiguration:
    database_url: str


def _select_database(
    db_path: str | Path | None, database_uri: str | SecretStr | None
) -> tuple[Path | None, str]:
    if (db_path is None) == (database_uri is None):
        raise ValueError("exactly one of db_path or database_uri is required")
    if db_path is not None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path, f"sqlite:///{path}"
    if isinstance(database_uri, SecretStr):
        return None, database_uri.get_secret_value()
    assert database_uri is not None
    return None, database_uri


def create_app(
    db_path: str | Path | None = None,
    *,
    database_uri: str | SecretStr | None = None,
    api_key: str | SecretStr | None = None,
) -> FastAPI:
    """Create the recap FastAPI application.

    Args:
        db_path: Path to the SQLite database file. Created if it doesn't exist.
        database_uri: PostgreSQL SQLAlchemy URL. Mutually exclusive with db_path.
        api_key: Optional key that enables authentication on every route.

    Returns:
        Configured FastAPI application with REST command endpoints.
    """
    _, database_url = _select_database(db_path, database_uri)
    database_config = _DatabaseConfiguration(database_url)
    apply_migrations(database_url)
    engine, session_factory = create_engine_and_session_factory(database_config)
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(
        title="recap REST server",
        description="REST API for recap experiment provenance data.",
        version="1.0.0",
        dependencies=[Depends(authenticate_request)] if api_key is not None else None,
        lifespan=lifespan,
    )
    app.state.session_factory = session_factory
    register_command_error_handlers(app)

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

    app.include_router(rest_router)

    return app
