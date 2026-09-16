from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request

from recap.adapter.local import LocalBackend


def get_local_backend(request: Request) -> Iterator[LocalBackend]:
    """Provide backend whose reads use short-lived sessions."""

    backend = LocalBackend(request.app.state.session_factory)
    yield backend
