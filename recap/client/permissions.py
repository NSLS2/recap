from enum import Enum

from pydantic import BaseModel, ConfigDict

from recap.authentication.models import ProviderIdentity
from recap.authorization.scopes import Scope
from recap.authorization.snapshot import GrantProvenance


class DenialCode(str, Enum):
    INSUFFICIENT_SCOPE = "insufficient_scope"
    INVALID_TARGET_RELATIONSHIP = "invalid_target_relationship"
    SOURCE_READ_REQUIRED = "source_read_required"
    DESTINATION_WRITE_REQUIRED = "destination_write_required"


class EffectivePermissions(BaseModel):
    model_config = ConfigDict(frozen=True)

    identities: tuple[ProviderIdentity, ...]
    snapshot_generation: str | None
    effective_scopes: frozenset[Scope]
    matched_namespace_paths: tuple[str, ...]
    grants: tuple[GrantProvenance, ...]


class ActorPermissions(BaseModel):
    """Current actor's effective permissions returned by remote APIs."""

    model_config = ConfigDict(frozen=True)

    identities: tuple[ProviderIdentity, ...]
    snapshot_generation: str | None
    effective_scopes: frozenset[Scope]
    matched_namespace_paths: tuple[str, ...]
    groups: tuple[str, ...]
    roles: tuple[str, ...]


class PermissionDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed: bool
    denial_code: DenialCode | None = None
    permissions: EffectivePermissions | None = None
