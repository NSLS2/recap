from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from recap.schemas.resource import ResourceCopyOptions

if TYPE_CHECKING:
    from recap.client.base_client import RecapClient


@dataclass(frozen=True, slots=True)
class NamespaceClient:
    """Namespace-scoped facade over :class:`~recap.client.RecapClient`.

    All builders and queries created by this facade use ``path`` as namespace
    context. Local clients persist on clean context-manager exit; remote clients
    send commands to the configured service. Invalid namespace or builder
    arguments raise ``ValueError`` or ``TypeError`` from the underlying client.
    """

    client: RecapClient
    path: str

    @property
    def _view(self) -> RecapClient:
        return self.client.namespace(self.path)

    def query_maker(self, *, on_unloaded: str = "warn"):
        """Return immutable query factories scoped to this namespace."""
        return self._view.query_maker(on_unloaded=on_unloaded)

    def build_resource_template(
        self,
        *,
        name: str | None = None,
        type_names: list[str] | None = None,
        version: str = "1.0",
        resource_template_id: UUID | None = None,
        on_existing: Literal["silent", "warn", "raise"] = "warn",
    ):
        """Open resource-template builder; clean exit persists, exceptions roll back."""
        return self._view.build_resource_template(
            name=name,
            type_names=type_names,
            version=version,
            resource_template_id=resource_template_id,
            on_existing=on_existing,
        )

    def build_process_template(
        self,
        name: str | None = None,
        version: str | None = None,
        *,
        process_template_id: UUID | None = None,
        on_existing: Literal["silent", "warn", "raise"] = "warn",
    ):
        """Open process-template builder scoped to this namespace."""
        if process_template_id is not None:
            return self._view.build_process_template(
                process_template_id=process_template_id,
                on_existing=on_existing,
            )
        return self._view.build_process_template(
            name,
            version,
            on_existing=on_existing,
        )

    def build_resource(
        self,
        name: str | None = None,
        template_name: str | None = None,
        template_version: str = "1.0",
        *,
        resource_id: UUID | None = None,
        on_existing: Literal["create", "silent", "warn", "raise"] = "warn",
        parent=None,
    ):
        """Open resource builder, optionally loading existing UUID."""
        if resource_id is not None:
            return self._view.build_resource(
                resource_id=resource_id,
                on_existing=on_existing,
            )
        return self._view.build_resource(
            name,
            template_name,
            template_version,
            on_existing=on_existing,
            parent=parent,
        )

    def build_process_run(
        self,
        name: str | None = None,
        description: str | None = None,
        template_name: str | None = None,
        version: str | None = None,
        *,
        process_run_id: UUID | None = None,
        on_existing: Literal["silent", "warn", "raise"] = "warn",
    ):
        """Open process-run builder, optionally loading existing UUID."""
        if process_run_id is not None:
            return self._view.build_process_run(
                process_run_id=process_run_id,
                on_existing=on_existing,
                namespace_path=self.path,
            )
        return self._view.build_process_run(
            name,
            description,
            template_name,
            version,
            on_existing=on_existing,
        )

    def copy_resource(
        self,
        *,
        source_resource_id: UUID,
        changes: ResourceCopyOptions | dict[str, Any] | None = None,
    ):
        """Copy resource into this namespace, validating copy options first."""
        options = (
            changes
            if isinstance(changes, ResourceCopyOptions)
            else ResourceCopyOptions.model_validate(changes or {})
        )
        return self._view.copy_resource(
            source_resource_id,
            options=options,
        )

    def create(self, metadata: dict[str, Any] | None = None):
        """Create namespace metadata; local writes persist through client backend."""
        return self.client.create_namespace(self.path, metadata)
