from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from recap.commands.errors import CommandConflictError

from .base import Base


class IdempotencyRecord(Base):
    __tablename__ = "command_idempotency"

    actor_id: Mapped[str] = mapped_column(String, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String, nullable=True)
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    create_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


@dataclass(frozen=True, slots=True)
class IdempotencyDecision:
    actor_id: str
    idempotency_key: str
    fingerprint: str
    replayed: bool
    target_id: str | None = None
    response: dict[str, Any] | None = None


class IdempotencyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def claim(
        self,
        actor_id: str,
        idempotency_key: str,
        fingerprint: str,
        authorize_replay: Callable[[str | None], None],
    ) -> IdempotencyDecision:
        table = IdempotencyRecord.__table__
        values = {
            "actor_id": actor_id,
            "idempotency_key": idempotency_key,
            "fingerprint": fingerprint,
        }
        dialect = self._session.get_bind().dialect.name
        if dialect == "postgresql":
            statement = postgresql_insert(table).values(**values)
        elif dialect == "sqlite":
            statement = sqlite_insert(table).values(**values)
        else:  # pragma: no cover - application engine rejects other dialects
            raise ValueError(f"Unsupported database dialect: {dialect}")
        statement = statement.on_conflict_do_nothing(
            index_elements=[table.c.actor_id, table.c.idempotency_key]
        )

        inserted = self._session.execute(statement).rowcount == 1
        if inserted:
            return IdempotencyDecision(
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
                replayed=False,
            )

        record = self._session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.actor_id == actor_id,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
        )
        if record is None:  # pragma: no cover - guarded by atomic insert
            raise RuntimeError("Idempotency claim disappeared")
        if record.fingerprint != fingerprint:
            raise CommandConflictError(
                "Idempotency key was already used for a different command"
            )
        if record.response_json is None:
            raise CommandConflictError("Idempotency command is still in progress")

        authorize_replay(record.target_id)
        return IdempotencyDecision(
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
            replayed=True,
            target_id=record.target_id,
            response=dict(record.response_json),
        )

    def complete(
        self,
        decision: IdempotencyDecision,
        *,
        target_id: str | None,
        response: Mapping[str, Any],
    ) -> None:
        if decision.replayed:
            raise CommandConflictError(
                "Replayed idempotency command is already complete"
            )
        result = self._session.execute(
            update(IdempotencyRecord)
            .where(
                IdempotencyRecord.actor_id == decision.actor_id,
                IdempotencyRecord.idempotency_key == decision.idempotency_key,
                IdempotencyRecord.fingerprint == decision.fingerprint,
                IdempotencyRecord.response_json.is_(None),
            )
            .values(target_id=target_id, response_json=dict(response))
        )
        if result.rowcount != 1:
            raise CommandConflictError("Idempotency command could not be completed")
