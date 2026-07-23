"""FastAPI application factory for the recap GraphQL server."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from recap.client import RecapClient
from recap.server.strawberry_schema import build_router


def create_app(db_path: str | Path) -> FastAPI:
    """Create the recap FastAPI application.

    Args:
        db_path: Path to the SQLite database file. Created if it doesn't exist.

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
