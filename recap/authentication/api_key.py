from __future__ import annotations

import hashlib
import secrets

from pydantic import SecretStr

from recap.authentication.actors import single_user_actor
from recap.authentication.errors import InvalidCredentialsError
from recap.authentication.models import RequestActor


class ApiKeyRequestAuthenticator:
    """Authenticate one configured API key without exposing it."""

    def __init__(self, api_key: str | SecretStr) -> None:
        self._api_key = (
            api_key if isinstance(api_key, SecretStr) else SecretStr(api_key)
        )
        secret = self._api_key.get_secret_value()
        self._actor = single_user_actor(
            credential_fingerprint=hashlib.sha256(secret.encode()).hexdigest(),
            provider="api-key",
        )

    async def authenticate(self, credential: object) -> RequestActor:
        if not isinstance(credential, str) or not secrets.compare_digest(
            credential, self._api_key.get_secret_value()
        ):
            raise InvalidCredentialsError("Invalid API key")
        return self._actor

    def __repr__(self) -> str:
        return f"{type(self).__name__}(api_key=SecretStr('**********'))"
