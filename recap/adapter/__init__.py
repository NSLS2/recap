from typing import Literal, Protocol, overload, runtime_checkable
from uuid import UUID

from pydantic import BaseModel

from recap.client.permissions import ActorPermissions
from recap.commands.models import CommandContext, CommandModel
from recap.dsl.query import QuerySpec, SchemaT
from recap.schemas.process import (
    ProcessRunSchema,
    ProcessTemplateRef,
    ProcessTemplateSchema,
)
from recap.schemas.resource import (
    ResourceSchema,
    ResourceTemplateRef,
    ResourceTemplateSchema,
)
from recap.schemas.step import StepSchema


class UnitOfWork(Protocol):
    def commit(self, clear_session: bool = True) -> None: ...
    def rollback(self) -> None: ...
    def end_session(self) -> None: ...


@runtime_checkable
class ReadBackend(Protocol):
    """Read-only backend contract. Implemented by LocalBackend and GraphQLAdapter."""

    def query(
        self, schema: type[SchemaT], spec: QuerySpec, *, namespace_path: str
    ) -> list[SchemaT]: ...
    def count(
        self, schema: type[SchemaT], spec: QuerySpec, *, namespace_path: str
    ) -> int: ...
    @overload
    def get_process_template(
        self,
        namespace_id: UUID,
        name: str | None,
        version: str | None,
        expand: Literal[False],
        id: UUID | str | None = None,
    ) -> ProcessTemplateRef: ...

    @overload
    def get_process_template(
        self,
        namespace_id: UUID,
        name: str | None,
        version: str | None,
        expand: Literal[True],
        id: UUID | str | None = None,
    ) -> ProcessTemplateSchema: ...

    @overload
    def get_resource_template(
        self,
        namespace_id: UUID,
        name: str | None,
        version: str | None = None,
        id: UUID | str | None = None,
        parent: ResourceTemplateRef | ResourceTemplateSchema | None = None,
        expand: Literal[False] = False,
    ) -> ResourceTemplateRef: ...

    @overload
    def get_resource_template(
        self,
        namespace_id: UUID,
        name: str | None,
        version: str | None = None,
        id: UUID | str | None = None,
        parent: ResourceTemplateRef | ResourceTemplateSchema | None = None,
        expand: Literal[True] = False,
    ) -> ResourceTemplateSchema: ...

    def get_resource(
        self,
        namespace_id: UUID,
        name: str,
        template_name: str,
        template_version: str | None = "1.0",
        expand: bool = False,
    ) -> ResourceSchema: ...

    def find_resources_by_identity(
        self,
        namespace_id: UUID,
        name: str,
        parent_id: UUID | None,
        resource_template_id: UUID,
    ) -> list: ...

    def get_steps(self, process_run: ProcessRunSchema) -> list[StepSchema]: ...
    def get_params(self, step_schema: StepSchema) -> type[BaseModel]: ...


@runtime_checkable
class PermissionsBackend(Protocol):
    def permissions(self, namespace_path: str) -> ActorPermissions: ...


@runtime_checkable
class WriteBackend(Protocol):
    """Write contract accepting closed commands plus trusted request context."""

    def execute(self, command: CommandModel, context: CommandContext) -> object: ...


@runtime_checkable
class Backend(ReadBackend, WriteBackend, Protocol):
    """Combined read+write protocol. Implemented by LocalBackend."""

    def list_child_namespace_paths(self, parent_path: str) -> list[str]: ...
