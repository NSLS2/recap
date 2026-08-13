from __future__ import annotations

from uuid import uuid4

from recap.authentication.actors import single_user_actor
from recap.authorization.policy import UnrestrictedNamespacePolicy
from recap.commands.models import CommandContext
from recap.server.audit import AuditRecord, AuditSink


class DiscardAuditSink:
    def emit(self, record: AuditRecord) -> None:
        pass


def build_local_command_context(
    *,
    audit_sink: AuditSink | None = None,
    request_id: str | None = None,
    idempotency_key: str | None = None,
) -> CommandContext:
    return CommandContext(
        actor=single_user_actor(credential_fingerprint="local-single-user"),
        request_id=request_id or str(uuid4()),
        policy=UnrestrictedNamespacePolicy(),
        audit_sink=audit_sink or DiscardAuditSink(),
        authorization_generation=None,
        idempotency_key=idempotency_key,
    )


__all__ = ["DiscardAuditSink", "build_local_command_context"]
