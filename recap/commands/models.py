from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from recap.authentication.models import RequestActor
from recap.authorization.query import NamespacePolicy
from recap.dsl.drafts import ProcessTemplateDraft
from recap.server.audit import AuditSink


class CommandModel(BaseModel):
    """Closed, immutable base for client-controlled command payloads."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CreateResource(CommandModel):
    namespace_path: str
    name: str
    template_id: UUID


class CreateProcessTemplate(CommandModel):
    namespace_path: str
    draft: ProcessTemplateDraft


class UpdateProcessTemplate(CommandModel):
    template_id: UUID
    expected_revision: int
    draft: ProcessTemplateDraft


@dataclass(frozen=True, slots=True)
class CommandContext:
    actor: RequestActor
    request_id: str
    policy: NamespacePolicy
    audit_sink: AuditSink
    authorization_generation: str | None
    idempotency_key: str | None = None
