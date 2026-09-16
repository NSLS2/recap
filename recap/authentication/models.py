from enum import Enum

from pydantic import BaseModel, ConfigDict

from recap.authorization.scopes import Scope


class ActorKind(str, Enum):
    USER = "user"
    SERVICE = "service"


class ProviderIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    subject: str


class IdentityEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    identity: ProviderIdentity


class RequestActor(BaseModel):
    model_config = ConfigDict(frozen=True)

    actor_id: str
    kind: ActorKind
    identities: tuple[ProviderIdentity, ...]
    credential_scopes: frozenset[Scope]
    namespace_restrictions: frozenset[str] | None
    credential_fingerprint: str
