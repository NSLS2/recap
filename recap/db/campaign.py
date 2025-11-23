import typing
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, UniqueConstraint
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from recap.db.base import Base, TimestampMixin

if typing.TYPE_CHECKING:
    from recap.db.process import ProcessRun
    from recap.db.resource import Resource


class Campaign(TimestampMixin, Base):
    __tablename__ = "campaign"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)
    proposal: Mapped[str] = mapped_column(nullable=False)
    saf: Mapped[str] = mapped_column(nullable=True)
    meta_data: Mapped[dict[str, Any] | None] = mapped_column(
        MutableDict.as_mutable(JSON), nullable=True
    )
    process_runs: Mapped[list["ProcessRun"]] = relationship(
        "ProcessRun", back_populates="campaign"
    )
    resources: Mapped[list["Resource"]] = relationship(
        "Resource", back_populates="campaigns"
    )
    __table_args__ = (
        UniqueConstraint("name", "proposal", name="uq_campaign_name_proposal"),
    )
