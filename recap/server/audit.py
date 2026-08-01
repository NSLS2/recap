"""Sanitized mutation audit contract."""

from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from recap.client.permissions import DenialCode
from recap.server.errors import ErrorCode


class AuditOutcome(str, Enum):
    SUCCESS = "success"
    DENIED = "denied"
    ERROR = "error"


class AuditRecord(BaseModel):
    """Mutation metadata safe for audit storage.

    Payload values, request headers, credentials, and authorization grant details
    have no representation in this closed model.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    actor_id: str | None
    mutation: str
    resource_type: str
    resource_id: str | None
    outcome: AuditOutcome
    reason_code: DenialCode | ErrorCode | None = None

    @model_validator(mode="after")
    def validate_reason_code(self) -> AuditRecord:
        if self.outcome is AuditOutcome.SUCCESS and self.reason_code is not None:
            raise ValueError("Successful audit records cannot have a reason code")
        if self.outcome is AuditOutcome.DENIED and not isinstance(
            self.reason_code, DenialCode
        ):
            raise ValueError("Denied audit records require a denial code")
        if self.outcome is AuditOutcome.ERROR and not isinstance(
            self.reason_code, ErrorCode
        ):
            raise ValueError("Error audit records require an error code")
        return self


@runtime_checkable
class AuditSink(Protocol):
    """Destination for sanitized mutation audit records."""

    def emit(self, record: AuditRecord) -> None: ...
