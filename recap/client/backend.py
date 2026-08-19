from dataclasses import dataclass, field
from threading import RLock
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from recap.adapter import (
    NamespaceCatalog,
    NamespaceContextResolver,
    NamespaceWriter,
    PermissionsBackend,
    ReadBackend,
    WriteBackend,
)
from recap.client.identity import IdentityMap
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
    identity_map: IdentityMap = field(default_factory=IdentityMap)
    _close_lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)
    _closed: bool = field(default=False, init=False, repr=False, compare=False)
    _identity_cleared: bool = field(default=False, init=False, repr=False, compare=False)
    _closed_capabilities: set[int] = field(
        default_factory=set, init=False, repr=False, compare=False
    )

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
        models = self.reader.query(schema, spec, namespace_path=namespace_path)
        result = []
        for item in models:
            result.append(self.identity_map.intern(item))
        return result

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
            result = self.writer.execute(command, context)
        else:
            result = self.writer.execute(command, context, etag_override=etag_override)
        return (
            self.identity_map.intern(result, authoritative=True)
            if isinstance(result, BaseModel)
            else result
        )

    def close(self, *, clear_identity: bool = True) -> None:
        with self._close_lock:
            if self._closed:
                return
            first_error = self._clear_identity(clear_identity)
            capability_error = self._close_capabilities()
            if first_error is None:
                first_error = capability_error
            if first_error is not None:
                raise first_error
            object.__setattr__(self, "_closed", True)

    def _clear_identity(self, clear_identity: bool) -> Exception | None:
        if not clear_identity or getattr(self, "_identity_cleared", False):
            return None
        try:
            self.identity_map.clear()
        except Exception as error:
            return error
        object.__setattr__(self, "_identity_cleared", True)
        return None

    def _close_capabilities(self) -> Exception | None:
        closed_capabilities = getattr(self, "_closed_capabilities", None)
        if closed_capabilities is None:
            closed_capabilities = set()
            object.__setattr__(self, "_closed_capabilities", closed_capabilities)
        attempted: set[int] = set()
        first_error: Exception | None = None
        for capability in (
            self.reader,
            self.writer,
            self.namespaces,
            self.namespace_writer,
            getattr(self, "context_resolver", None),
            getattr(self, "permissions", None),
        ):
            if capability is None:
                continue
            identity = id(capability)
            if identity in closed_capabilities or identity in attempted:
                continue
            attempted.add(identity)
            close = getattr(capability, "close", None)
            if close is not None:
                try:
                    close()
                except Exception as error:
                    if first_error is None:
                        first_error = error
                    continue
                closed_capabilities.add(identity)
        return first_error


def require_capability(value: Any, protocol: type, name: str) -> None:
    if not isinstance(value, protocol):
        raise TypeError(f"{name} must satisfy {protocol.__name__}")
