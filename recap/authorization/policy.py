from recap.authentication.models import RequestActor
from recap.authorization.scopes import Scope
from recap.authorization.snapshot import AuthorizationSnapshot, GrantProvenance
from recap.client.permissions import (
    DenialCode,
    EffectivePermissions,
    PermissionDecision,
)
from recap.utils.namespace import (
    canonicalize_namespace_path,
    is_namespace_ancestor,
    namespace_ancestors,
    parent_namespace_path,
)


def _grant_key(grant: GrantProvenance) -> tuple[str, str, str, str, str, str]:
    return (
        grant.namespace_path,
        grant.scope.value,
        grant.identity.provider,
        grant.identity.subject,
        grant.group,
        grant.role,
    )


def _denied(code: DenialCode) -> PermissionDecision:
    return PermissionDecision(allowed=False, denial_code=code)


class SnapshotNamespacePolicy:
    def __init__(self, snapshot: AuthorizationSnapshot) -> None:
        self._snapshot = snapshot

    def permissions_for(
        self, actor: RequestActor, namespace_path: str
    ) -> EffectivePermissions:
        path = canonicalize_namespace_path(namespace_path)
        restrictions = (
            None
            if actor.namespace_restrictions is None
            else tuple(
                canonicalize_namespace_path(item)
                for item in actor.namespace_restrictions
            )
        )
        ancestors = namespace_ancestors(path)

        if restrictions is not None and not any(
            is_namespace_ancestor(restriction, path) for restriction in restrictions
        ):
            matched_grants: tuple[GrantProvenance, ...] = ()
        else:
            identities = set(actor.identities)
            matched_grants = tuple(
                sorted(
                    (
                        grant
                        for grant in self._snapshot.grants
                        if grant.namespace_path in ancestors
                        and grant.identity in identities
                        and grant.scope in actor.credential_scopes
                    ),
                    key=_grant_key,
                )
            )

        matched_paths = tuple(
            ancestor
            for ancestor in ancestors
            if any(grant.namespace_path == ancestor for grant in matched_grants)
        )
        return EffectivePermissions(
            identities=actor.identities,
            snapshot_generation=self._snapshot.metadata.source_revision,
            effective_scopes=frozenset(grant.scope for grant in matched_grants),
            matched_namespace_paths=matched_paths,
            grants=matched_grants,
        )

    def authorize(
        self, actor: RequestActor, scope: Scope, namespace_path: str
    ) -> PermissionDecision:
        permissions = self.permissions_for(actor, namespace_path)
        if scope not in permissions.effective_scopes:
            return _denied(DenialCode.INSUFFICIENT_SCOPE)
        return PermissionDecision(allowed=True, permissions=permissions)

    def authorize_process_run(
        self,
        actor: RequestActor,
        scope: Scope,
        *,
        context_namespace_path: str,
        target_namespace_path: str,
    ) -> PermissionDecision:
        context = canonicalize_namespace_path(context_namespace_path)
        target = canonicalize_namespace_path(target_namespace_path)
        decision = self.authorize(actor, scope, context)
        if not decision.allowed:
            return decision
        if target != context:
            return _denied(DenialCode.INVALID_TARGET_RELATIONSHIP)
        return decision

    def authorize_template(
        self,
        actor: RequestActor,
        scope: Scope,
        *,
        context_namespace_path: str,
        target_namespace_path: str,
    ) -> PermissionDecision:
        return self._authorize_ancestor_visible(
            actor,
            scope,
            context_namespace_path=context_namespace_path,
            target_namespace_path=target_namespace_path,
        )

    def authorize_resource(
        self,
        actor: RequestActor,
        scope: Scope,
        *,
        context_namespace_path: str,
        target_namespace_path: str,
    ) -> PermissionDecision:
        return self._authorize_ancestor_visible(
            actor,
            scope,
            context_namespace_path=context_namespace_path,
            target_namespace_path=target_namespace_path,
        )

    def _authorize_ancestor_visible(
        self,
        actor: RequestActor,
        scope: Scope,
        *,
        context_namespace_path: str,
        target_namespace_path: str,
    ) -> PermissionDecision:
        context = canonicalize_namespace_path(context_namespace_path)
        target = canonicalize_namespace_path(target_namespace_path)
        decision = self.authorize(actor, scope, context)
        if not decision.allowed:
            return decision
        is_read = scope.value.endswith(":read")
        valid_target = (
            is_namespace_ancestor(target, context) if is_read else target == context
        )
        if not valid_target:
            return _denied(DenialCode.INVALID_TARGET_RELATIONSHIP)
        return decision

    def authorize_namespace_create(
        self, actor: RequestActor, namespace_path: str
    ) -> PermissionDecision:
        path = canonicalize_namespace_path(namespace_path)
        return self.authorize(actor, Scope.NAMESPACE_WRITE, parent_namespace_path(path))

    def authorize_copy(
        self,
        actor: RequestActor,
        *,
        source_namespace_path: str,
        destination_namespace_path: str,
    ) -> PermissionDecision:
        source = canonicalize_namespace_path(source_namespace_path)
        destination = canonicalize_namespace_path(destination_namespace_path)
        source_decision = self.authorize(actor, Scope.RESOURCE_READ, source)
        if not source_decision.allowed:
            return _denied(DenialCode.SOURCE_READ_REQUIRED)
        destination_decision = self.authorize(actor, Scope.RESOURCE_WRITE, destination)
        if not destination_decision.allowed:
            return _denied(DenialCode.DESTINATION_WRITE_REQUIRED)

        source_permissions = source_decision.permissions
        destination_permissions = destination_decision.permissions
        assert source_permissions is not None
        assert destination_permissions is not None
        grants = tuple(
            sorted(
                set(source_permissions.grants) | set(destination_permissions.grants),
                key=_grant_key,
            )
        )
        permissions = EffectivePermissions(
            identities=actor.identities,
            snapshot_generation=source_permissions.snapshot_generation,
            effective_scopes=(
                source_permissions.effective_scopes
                | destination_permissions.effective_scopes
            ),
            matched_namespace_paths=tuple(
                dict.fromkeys(
                    source_permissions.matched_namespace_paths
                    + destination_permissions.matched_namespace_paths
                )
            ),
            grants=grants,
        )
        return PermissionDecision(allowed=True, permissions=permissions)


class UnrestrictedNamespacePolicy(SnapshotNamespacePolicy):
    def __init__(self) -> None:
        pass

    def permissions_for(
        self, actor: RequestActor, namespace_path: str
    ) -> EffectivePermissions:
        canonicalize_namespace_path(namespace_path)
        return EffectivePermissions(
            identities=actor.identities,
            snapshot_generation=None,
            effective_scopes=frozenset(Scope),
            matched_namespace_paths=(),
            grants=(),
        )


__all__ = [
    "DenialCode",
    "EffectivePermissions",
    "PermissionDecision",
    "SnapshotNamespacePolicy",
    "UnrestrictedNamespacePolicy",
]
