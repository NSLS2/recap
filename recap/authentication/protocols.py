from typing import Protocol

from .models import IdentityEvidence, RequestActor


class InternalAuthenticator(Protocol):
    async def authenticate(self, credential: object) -> IdentityEvidence: ...


class ExternalAuthenticator(Protocol):
    async def authenticate(self, credential: object) -> IdentityEvidence: ...


class RequestAuthenticator(Protocol):
    async def authenticate(self, credential: object) -> RequestActor: ...
