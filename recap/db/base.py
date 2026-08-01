from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, func, update
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from recap.commands.errors import CommandConflictError
from recap.lifecycle import LifecycleStatus


class Base(DeclarativeBase):
    pass


class RevisionedLifecycleMixin:
    status: Mapped[LifecycleStatus] = mapped_column(
        Enum(LifecycleStatus, name="lifecyclestatus"),
        nullable=False,
        default=LifecycleStatus.MUTABLE,
    )
    revision: Mapped[int] = mapped_column(nullable=False, default=1)


def compare_and_swap_revision(
    session: Session,
    model: type[RevisionedLifecycleMixin],
    entity_id: Any,
    *,
    expected_revision: int,
    values: Mapping[str, Any],
) -> int:
    """Apply one update only when caller's expected revision is current."""
    if "revision" in values:
        raise ValueError("Revision is managed by compare-and-swap")
    result = session.execute(
        update(model)
        .where(model.id == entity_id, model.revision == expected_revision)
        .values(**values, revision=model.revision + 1)
    )
    if result.rowcount != 1:
        raise CommandConflictError("Expected revision is stale")
    return expected_revision + 1


# Reusable timestamps
class TimestampMixin:
    # Timestamp when the row is first inserted (set by the DB)
    create_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),  # let the DB populate on INSERT
        nullable=False,
    )

    # Timestamp when the row was last modified (auto-updates on UPDATE)
    modified_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),  # initial value on INSERT
        onupdate=func.now(),  # emit NOW() on UPDATE
        nullable=False,
    )
