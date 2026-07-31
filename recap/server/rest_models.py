from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from recap.lifecycle import LifecycleStatus
from recap.schemas.resource import ResourceCopyOptions


class RestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateNamespaceRequest(RestModel):
    metadata: dict[str, Any] = Field(default_factory=dict)


class UpdateNamespaceRequest(RestModel):
    metadata: dict[str, Any] | None = None
    status: LifecycleStatus | None = None

    @model_validator(mode="after")
    def require_change(self):
        if self.metadata is None and self.status is None:
            raise ValueError("Namespace update is empty")
        return self


class CreateResourceRequest(RestModel):
    name: str
    template_id: str
    parent_id: str | None = None


class UpdateResourceRequest(RestModel):
    name: str | None = None
    properties: dict[str, dict[str, Any]] | None = None

    @model_validator(mode="after")
    def require_change(self):
        if self.name is None and not self.properties:
            raise ValueError("Resource update is empty")
        return self


class CopyResourceRequest(RestModel, ResourceCopyOptions):
    pass
