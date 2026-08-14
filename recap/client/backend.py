from dataclasses import dataclass
from typing import Any
from uuid import UUID

from recap.adapter import (
    NamespaceCatalog,
    NamespaceContextResolver,
    NamespaceWriter,
    PermissionsBackend,
    ReadBackend,
    WriteBackend,
)
from recap.commands.models import CommandContext, CommandModel
from recap.dsl.query import QuerySpec, SchemaT
from recap.lifecycle import LifecycleStatus


@dataclass(frozen=True, slots=True)
class ClientBackend:
    reader: ReadBackend
    writer: WriteBackend
    namespaces: NamespaceCatalog
    namespace_writer: NamespaceWriter
    context_resolver: NamespaceContextResolver | None = None
    permissions: PermissionsBackend | None = None

    def __post_init__(self) -> None:
        require_capability(self.reader, ReadBackend, "reader")
        require_capability(self.writer, WriteBackend, "writer")
        require_capability(self.namespaces, NamespaceCatalog, "namespaces")
        require_capability(self.namespace_writer, NamespaceWriter, "namespace_writer")
        if self.context_resolver is not None:
            require_capability(self.context_resolver, NamespaceContextResolver, "context_resolver")
        if self.permissions is not None:
            require_capability(self.permissions, PermissionsBackend, "permissions")

    def query(
        self, schema: type[SchemaT], spec: QuerySpec, *, namespace_path: str
    ) -> list[SchemaT]:
        return self.reader.query(schema, spec, namespace_path=namespace_path)

    def count(self, schema: type[SchemaT], spec: QuerySpec, *, namespace_path: str) -> int:
        return self.reader.count(schema, spec, namespace_path=namespace_path)

    def list_child_namespaces(self, parent_path: str) -> list[str]:
        return self.namespaces.list_child_namespaces(parent_path)

    def create_namespace(
        self, path: str, metadata: dict[str, Any] | None, context: CommandContext
    ):
        return self.namespace_writer.create_namespace(path, metadata, context)

    def update_namespace(
        self,
        namespace_id: UUID,
        expected_revision: int,
        metadata: dict[str, Any] | None,
        status: LifecycleStatus | None,
        context: CommandContext,
        *,
        etag: str | None = None,
    ):
        return self.namespace_writer.update_namespace(
            namespace_id,
            expected_revision,
            metadata,
            status,
            context,
            etag=etag,
        )

    def _execute(
        self,
        command: CommandModel,
        context: CommandContext,
        *,
        etag_override: str | None = None,
    ) -> object:
        if etag_override is None:
            return self.writer.execute(command, context)
        return self.writer.execute(command, context, etag_override=etag_override)

    def close(self) -> None:
        closed: set[int] = set()
        for capability in (
            self.reader,
            self.writer,
            self.namespaces,
            self.namespace_writer,
        ):
            identity = id(capability)
            if identity in closed:
                continue
            close = getattr(capability, "close", None)
            if close is not None:
                close()
            closed.add(identity)


def require_capability(value: Any, protocol: type, name: str) -> None:
    if not isinstance(value, protocol):
        raise TypeError(f"{name} must satisfy {protocol.__name__}")
