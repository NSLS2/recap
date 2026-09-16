from __future__ import annotations

import re
from typing import Annotated

from fastapi import Header, HTTPException, Request, status

from recap.authentication.errors import AuthenticationError
from recap.authentication.models import RequestActor

_API_KEY_HEADER = re.compile(r"Apikey ([^\s]+)")


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API key",
        headers={"WWW-Authenticate": "Apikey"},
    )


async def authenticate_request(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> RequestActor:
    """Authenticate an exact ``Apikey <credential>`` authorization header."""
    match = _API_KEY_HEADER.fullmatch(authorization or "")
    if match is None:
        raise _unauthorized()

    try:
        return await request.app.state.request_authenticator.authenticate(
            match.group(1)
        )
    except AuthenticationError:
        raise _unauthorized() from None
