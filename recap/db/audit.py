from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, String, Uuid, func
from sqlalchemy.orm import Mapped, Session, mapped_column

from recap.server.audit import AuditRecord

from .base import Base


class MutationAudit(Base):
    __tablename__ = "mutation_audit"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    request_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    mutation: Mapped[str] = mapped_column(String, nullable=False)
    resource_type: Mapped[str] = mapped_column(String, nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String, nullable=True)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String, nullable=True)
    create_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def as_record(self) -> AuditRecord:
        return AuditRecord(
            request_id=self.request_id,
            actor_id=self.actor_id,
            mutation=self.mutation,
            resource_type=self.resource_type,
            resource_id=self.resource_id,
            outcome=self.outcome,
            reason_code=self.reason_code,
        )


class MutationAuditRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, record: AuditRecord) -> None:
        self._session.add(
            MutationAudit(
                request_id=record.request_id,
                actor_id=record.actor_id,
                mutation=record.mutation,
                resource_type=record.resource_type,
                resource_id=record.resource_id,
                outcome=record.outcome.value,
                reason_code=record.reason_code.value if record.reason_code else None,
            )
        )
