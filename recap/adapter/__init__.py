from typing import TYPE_CHECKING, Protocol, runtime_checkable

from recap.client.permissions import ActorPermissions
from recap.commands.models import CommandContext, CommandModel
from recap.dsl.query import QuerySpec, SchemaT

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
class NamespaceChildrenBackend(Protocol):
    """Local namespace-child listing capability."""

    def list_child_namespace_paths(self, parent_path: str) -> list[str]: ...


@runtime_checkable
class PermissionsBackend(Protocol):
    def permissions(self, namespace_path: str) -> ActorPermissions: ...


@runtime_checkable
class WriteBackend(Protocol):
    """Write contract accepting closed commands plus trusted request context."""

    def execute(self, command: CommandModel, context: CommandContext) -> object: ...


@runtime_checkable
class Backend(ReadBackend, WriteBackend, NamespaceChildrenBackend, Protocol):
    """Combined read+write protocol. Implemented by LocalBackend."""
