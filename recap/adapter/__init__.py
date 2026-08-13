from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from uuid import UUID

from recap.client.permissions import ActorPermissions
from recap.commands.models import CommandContext, CommandModel
from recap.dsl.query import QuerySpec, SchemaT
from recap.lifecycle import LifecycleStatus
from recap.schemas.namespace import NamespaceContext

if TYPE_CHECKING:
    from recap.authorization.query import AuthorizedQuery


@runtime_checkable
class ReadBackend(Protocol):
    """Read-only backend contract. Implemented by LocalBackend and GraphQLAdapter."""

    def query(
        self, schema: type[SchemaT], spec: QuerySpec, *, namespace_path: str
    ) -> list[SchemaT]: ...
    def count(
        self, schema: type[SchemaT], spec: QuerySpec, *, namespace_path: str
    ) -> int: ...


@runtime_checkable
class AuthorizedReadBackend(ReadBackend, Protocol):
    """Server-only read contract with authorization-aware query operations."""

    def query_authorized(
        self,
        schema: type[SchemaT],
        spec: QuerySpec,
        *,
        authorization: "AuthorizedQuery",
    ) -> list[SchemaT]: ...

    def count_authorized(
        self,
        schema: type[SchemaT],
        spec: QuerySpec,
        *,
        authorization: "AuthorizedQuery",
    ) -> int: ...


@runtime_checkable
class NamespaceContextResolver(Protocol):
    def get_namespace_context(self, path: str) -> NamespaceContext: ...


@runtime_checkable
class NamespaceCatalog(Protocol):
    def list_child_namespaces(self, parent_path: str) -> list[str]: ...


@runtime_checkable
class PermissionsBackend(Protocol):
    def permissions(self, namespace_path: str) -> ActorPermissions: ...


@runtime_checkable
class WriteBackend(Protocol):
    """Write contract accepting closed commands plus trusted request context."""

    def execute(self, command: CommandModel, context: CommandContext) -> object: ...


@runtime_checkable
class NamespaceWriter(Protocol):
    def create_namespace(
        self,
        path: str,
        metadata: dict[str, Any] | None,
        context: CommandContext,
    ) -> NamespaceContext: ...

    def update_namespace(
        self,
        namespace_id: UUID,
        expected_revision: int,
        metadata: dict[str, Any] | None,
        status: LifecycleStatus | None,
        context: CommandContext,
        *,
        etag: str | None = None,
    ) -> NamespaceContext: ...
