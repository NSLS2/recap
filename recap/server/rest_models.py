from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from recap.dsl.drafts import ChildStepDraft, ProcessRunStepDraft
from recap.lifecycle import LifecycleStatus
from recap.schemas.process import ProcessRunCopyOptions
from recap.schemas.resource import ResourceChildDraft, ResourceCopyOptions


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
    id: UUID | None = None
    name: str
    template_id: str
    parent_id: str | None = None
    properties: dict[str, dict[str, Any]] | None = None
    children: tuple[ResourceChildDraft, ...] = ()


class UpdateResourceRequest(RestModel):
    name: str | None = None
    properties: dict[str, dict[str, Any]] | None = None
    in_place: bool = False
    children: tuple[ResourceChildDraft, ...] = ()

    @model_validator(mode="after")
    def require_change(self):
        if self.name is None and not self.properties and not self.children:
            raise ValueError("Resource update is empty")
        return self


class UpdateProcessRunRequest(RestModel):
    description: str | None = None
    status: str | None = None
    assignments: dict[str, UUID] | None = None
    steps: dict[str, ProcessRunStepDraft | dict[str, dict[str, object]]] | None = None
    in_place: bool = False
    child_steps: tuple[ChildStepDraft, ...] = ()

    @model_validator(mode="after")
    def require_change(self):
        if (
            self.description is None
            and self.status is None
            and self.assignments is None
            and self.steps is None
            and not self.child_steps
        ):
            raise ValueError("Process run update is empty")
        return self


class SetLifecycleStatusRequest(RestModel):
    status: LifecycleStatus


class CopyResourceRequest(RestModel, ResourceCopyOptions):
    destination_namespace: str


class CopyProcessRunRequest(RestModel, ProcessRunCopyOptions):
    destination_namespace: str
