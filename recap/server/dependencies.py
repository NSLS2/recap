from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request

from recap.adapter.local import LocalBackend


def get_local_backend(request: Request) -> Iterator[LocalBackend]:
    """Provide one transactional backend and session for one request."""

    backend = LocalBackend(request.app.state.session_factory)
    backend.begin()
    try:
        yield backend
    finally:
        backend.close()
