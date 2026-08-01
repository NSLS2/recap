from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import false
from sqlalchemy.sql import and_, or_

from recap.authentication.models import RequestActor
from recap.authorization.scopes import Scope
from recap.db.namespace import Namespace
from recap.db.process import ProcessRun, ProcessTemplate
from recap.db.resource import Resource, ResourceTemplate
from recap.lifecycle import LifecycleStatus

if TYPE_CHECKING:
    from recap.client.permissions import EffectivePermissions


class NamespacePolicy(Protocol):
    def permissions_for(
        self, actor: RequestActor, namespace_path: str
    ) -> EffectivePermissions: ...


_MODEL_SCOPES = {
    Namespace: (Scope.NAMESPACE_READ, Scope.NAMESPACE_WRITE),
    ProcessRun: (Scope.PROCESS_RUN_READ, Scope.PROCESS_RUN_WRITE),
    ProcessTemplate: (
        Scope.PROCESS_TEMPLATE_READ,
        Scope.PROCESS_TEMPLATE_WRITE,
    ),
    ResourceTemplate: (
        Scope.RESOURCE_TEMPLATE_READ,
        Scope.RESOURCE_TEMPLATE_WRITE,
    ),
    Resource: (Scope.RESOURCE_READ, Scope.RESOURCE_WRITE),
}


@dataclass(frozen=True, slots=True)
class AuthorizedQuery:
    namespace_path: str
    permissions: EffectivePermissions

    @classmethod
    def from_policy(
        cls,
        policy: NamespacePolicy,
        actor: RequestActor,
        *,
        namespace_path: str,
    ) -> AuthorizedQuery:
        return cls(
            namespace_path=namespace_path,
            permissions=policy.permissions_for(actor, namespace_path),
        )

    def apply(self, model, stmt, *, context_id, ancestor_ids):
        """Add actor visibility before caller-controlled query operations."""
        read_scope, write_scope = _MODEL_SCOPES[model]
        scopes = self.permissions.effective_scopes
        namespace_column = model.id if model is Namespace else model.namespace_id
        active_ids = [context_id] if model is ProcessRun else ancestor_ids
        predicates = []
        if read_scope in scopes:
            predicates.append(
                and_(
                    namespace_column.in_(active_ids),
                    model.status == LifecycleStatus.ACTIVE,
                )
            )
        if write_scope in scopes:
            predicates.append(
                and_(
                    namespace_column == context_id,
                    model.status == LifecycleStatus.MUTABLE,
                )
            )
        return stmt.where(or_(*predicates) if predicates else false())


__all__ = ["AuthorizedQuery", "NamespacePolicy"]
