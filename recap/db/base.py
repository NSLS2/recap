from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


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
