from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from recap.client.base_client import RecapClient


@dataclass(frozen=True, slots=True)
class NamespaceClient:
    client: RecapClient
    path: str

    def query_maker(self, *, on_unloaded: str = "warn"):
        return self.client.query_maker(namespace=self.path, on_unloaded=on_unloaded)

    def build_resource_template(self, **kwargs):
        return self.client.build_resource_template(namespace_path=self.path, **kwargs)

    def build_process_template(self, *args, **kwargs):
        return self.client.build_process_template(*args, namespace_path=self.path, **kwargs)

    def build_resource(self, *args, **kwargs):
        return self.client.build_resource(*args, namespace_path=self.path, **kwargs)

    def build_process_run(self, *args, **kwargs):
        return self.client.build_process_run(*args, namespace_path=self.path, **kwargs)

    def copy_resource(self, *, source_resource_id: UUID, changes: dict[str, Any] | None = None):
        return self.client.copy_resource(
            source_resource_id=source_resource_id,
            destination_namespace_path=self.path,
            changes=changes,
        )

    def create(self, metadata: dict[str, Any] | None = None):
        return self.client.create_namespace(self.path, metadata)
