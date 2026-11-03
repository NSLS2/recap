from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from recap.authentication.models import RequestActor
from recap.authorization.query import NamespacePolicy
from recap.dsl.drafts import (
    ChildStepDraft,
    ProcessRunDraft,
    ProcessRunStepDraft,
    ProcessTemplateDraft,
    ResourceTemplateDraft,
)
from recap.lifecycle import LifecycleStatus, ResourceFinalizeOptions
from recap.schemas.process import ProcessRunCopyOptions
from recap.schemas.resource import ResourceChildDraft, ResourceCopyOptions
from recap.server.audit import AuditSink


class CommandModel(BaseModel):
    """Closed, immutable base for client-controlled command payloads."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CreateResource(CommandModel):
    id: UUID | None = None
    namespace_path: str
    name: str
    template_id: UUID
    parent_id: UUID | None = None
    properties: dict[str, dict[str, object]] | None = None
    children: tuple[ResourceChildDraft, ...] = ()


class UpdateResource(CommandModel):
    resource_id: UUID
    expected_revision: int
    name: str | None = None
    properties: dict[str, dict[str, object]] | None = None
    in_place: bool = False
    children: tuple[ResourceChildDraft, ...] = ()


class CreateNamespace(CommandModel):
    path: str
    metadata: dict[str, Any] | None = None


class GetNamespace(CommandModel):
    path: str


class UpdateNamespace(CommandModel):
    namespace_id: UUID
    expected_revision: int
    metadata: dict[str, Any] | None = None
    status: LifecycleStatus | None = None


class CopyResource(CommandModel):
    source_resource_id: UUID
    destination_namespace_path: str
    options: ResourceCopyOptions = ResourceCopyOptions()


class CreateProcessTemplate(CommandModel):
    namespace_path: str
    draft: ProcessTemplateDraft


class UpdateProcessTemplate(CommandModel):
    template_id: UUID
    expected_revision: int
    draft: ProcessTemplateDraft


class CreateResourceTemplate(CommandModel):
    namespace_path: str
    draft: ResourceTemplateDraft


class UpdateResourceTemplate(CommandModel):
    template_id: UUID
    expected_revision: int
    draft: ResourceTemplateDraft


class CreateProcessRun(CommandModel):
    namespace_path: str
    draft: ProcessRunDraft


class UpdateProcessRun(CommandModel):
    process_run_id: UUID
    expected_revision: int
    description: str | None = None
    status: str | None = None
    assignments: dict[str, UUID] | None = None
    steps: dict[str, ProcessRunStepDraft | dict[str, dict[str, object]]] | None = None
    in_place: bool = False
    child_steps: tuple[ChildStepDraft, ...] = ()


class CopyProcessRun(CommandModel):
    source_process_run_id: UUID
    destination_namespace_path: str
    options: ProcessRunCopyOptions = ProcessRunCopyOptions()


class SetLifecycleStatus(CommandModel, ResourceFinalizeOptions):
    object_type: str
    object_id: UUID
    expected_revision: int
    status: str

    @model_validator(mode="after")
    def validate_resource_options(self):
        if self.recursive_depth != 0:
            if self.object_type != "resource":
                raise ValueError("Recursive lifecycle options require a resource")
            if self.status != LifecycleStatus.ACTIVE:
                raise ValueError("Recursive lifecycle options require ACTIVE status")
        return self


@dataclass(frozen=True, slots=True)
class CommandContext:
    actor: RequestActor
    request_id: str
    policy: NamespacePolicy
    audit_sink: AuditSink
    authorization_generation: str | None
    idempotency_key: str | None = None
