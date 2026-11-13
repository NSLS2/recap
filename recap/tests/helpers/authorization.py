"""Authorization grant and snapshot factories shared by namespace tests."""

from recap.authentication.models import ProviderIdentity
from recap.authorization.policy import SnapshotNamespacePolicy
from recap.authorization.scopes import Scope
from recap.authorization.snapshot import (
    AuthorizationSnapshot,
    GrantProvenance,
    SnapshotMetadata,
)


def grant(
    identity: ProviderIdentity,
    namespace_path: str,
    scope: Scope,
    *,
    group: str = "scientists",
    role: str = "member",
) -> GrantProvenance:
    return GrantProvenance(
        identity=identity,
        namespace_path=namespace_path,
        scope=scope,
        group=group,
        role=role,
    )


def policy(*grants: GrantProvenance) -> SnapshotNamespacePolicy:
    return SnapshotNamespacePolicy(
        AuthorizationSnapshot(
            metadata=SnapshotMetadata(format_version=1, source_revision="revision-7"),
            grants=frozenset(grants),
        )
    )
