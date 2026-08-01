from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from recap.schemas.resource import ResourceCopyOptions

if TYPE_CHECKING:
    from recap.client.base_client import RecapClient


@dataclass(frozen=True, slots=True)
class NamespaceClient:
    client: RecapClient
    path: str

    def query_maker(self, *, on_unloaded: str = "warn"):
        return self.client.query_maker(namespace=self.path, on_unloaded=on_unloaded)

    def build_resource_template(
        self,
        *,
        name: str | None = None,
        type_names: list[str] | None = None,
        version: str = "1.0",
        resource_template_id: UUID | None = None,
        on_existing: Literal["silent", "warn", "raise"] = "warn",
    ):
        return self.client.build_resource_template(
            name=name,
            type_names=type_names,
            version=version,
            resource_template_id=resource_template_id,
            on_existing=on_existing,
            namespace_path=self.path,
        )

    def build_process_template(
        self,
        name: str | None = None,
        version: str | None = None,
        *,
        process_template_id: UUID | None = None,
        on_existing: Literal["silent", "warn", "raise"] = "warn",
    ):
        if process_template_id is not None:
            return self.client.build_process_template(
                process_template_id=process_template_id,
                on_existing=on_existing,
                namespace_path=self.path,
            )
        return self.client.build_process_template(
            name,
            version,
            on_existing=on_existing,
            namespace_path=self.path,
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
        if resource_id is not None:
            return self.client.build_resource(
                resource_id=resource_id,
                on_existing=on_existing,
                namespace_path=self.path,
            )
        return self.client.build_resource(
            name,
            template_name,
            template_version,
            on_existing=on_existing,
            parent=parent,
            namespace_path=self.path,
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
        if process_run_id is not None:
            return self.client.build_process_run(
                process_run_id=process_run_id,
                on_existing=on_existing,
                namespace_path=self.path,
            )
        return self.client.build_process_run(
            name,
            description,
            template_name,
            version,
            on_existing=on_existing,
            namespace_path=self.path,
        )

    def copy_resource(
        self,
        *,
        source_resource_id: UUID,
        changes: ResourceCopyOptions | dict[str, Any] | None = None,
    ):
        options = (
            changes
            if isinstance(changes, ResourceCopyOptions)
            else ResourceCopyOptions.model_validate(changes or {})
        )
        return self.client.copy_resource(
            source_resource_id,
            options=options,
            destination_namespace_path=self.path,
        )

    def create(self, metadata: dict[str, Any] | None = None):
        return self.client.create_namespace(self.path, metadata)
