from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from recap.db.audit import MutationAuditRepository
from recap.server.audit import AuditRecord


class DurableAuditSink:
    def __init__(self, repository: MutationAuditRepository) -> None:
        self._repository = repository

    def emit(self, record: AuditRecord) -> None:
        self._repository.add(record)


def record_failure_after_rollback(
    session_factory: sessionmaker, record: AuditRecord
) -> None:
    """Persist failure metadata after caller's mutation transaction rolled back."""
    with session_factory.begin() as session:
        DurableAuditSink(MutationAuditRepository(session)).emit(record)
