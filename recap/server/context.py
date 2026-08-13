from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from strawberry.fastapi import BaseContext

from recap.adapter import AuthorizedReadBackend
from recap.authentication.actors import single_user_actor
from recap.authentication.models import RequestActor
from recap.authorization.policy import (
    SnapshotNamespacePolicy,
    UnrestrictedNamespacePolicy,
)
from recap.authorization.query import NamespacePolicy
from recap.server.errors import request_id_from
from recap.server.security import authenticate_request


@dataclass(frozen=True, slots=True)
class GraphQLContext:
    backend: AuthorizedReadBackend
    actor: RequestActor
    policy: NamespacePolicy
    request_id: str


class StrawberryGraphQLContext(BaseContext):
    """Carry immutable request state while Strawberry attaches transport fields."""

    def __init__(self, graphql: GraphQLContext) -> None:
        super().__init__()
        self.graphql = graphql

    @property
    def backend(self) -> AuthorizedReadBackend:
        return self.graphql.backend

    @property
    def actor(self) -> RequestActor:
        return self.graphql.actor

    @property
    def policy(self) -> NamespacePolicy:
        return self.graphql.policy

    @property
    def request_id(self) -> str:
        return self.graphql.request_id


_LOCAL_ACTOR = single_user_actor(credential_fingerprint="local-single-user")


async def graphql_context(
    request: Request, backend: AuthorizedReadBackend, authorization: str | None
) -> StrawberryGraphQLContext:
    authenticator = getattr(request.app.state, "request_authenticator", None)
    actor = (
        await authenticate_request(request, authorization)
        if authenticator is not None
        else _LOCAL_ACTOR
    )
    provider = getattr(request.app.state, "authorization_snapshot_provider", None)
    if provider is not None:
        policy: NamespacePolicy = SnapshotNamespacePolicy(provider.acquire())
    else:
        policy = getattr(
            request.app.state, "namespace_policy", UnrestrictedNamespacePolicy()
        )
    return StrawberryGraphQLContext(
        GraphQLContext(
            backend=backend,
            actor=actor,
            policy=policy,
            request_id=request_id_from(request),
        )
    )


__all__ = ["GraphQLContext", "StrawberryGraphQLContext", "graphql_context"]
