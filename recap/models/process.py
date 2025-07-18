from typing import List, Optional
from uuid import UUID, uuid4
import enum

from sqlalchemy import ForeignKey, Enum, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext import associationproxy

from recap.models.step import StepTemplate, StepTemplateEdge, Step
from recap.models.resource import Resource

from .base import Base


class Direction(str, enum.Enum):
    input = "input"
    output = "output"


class ProcessTemplate(Base):
    __tablename__ = "process_template"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)
    step_templates: Mapped[List["StepTemplate"]] = relationship(
        back_populates="process_template"
    )
    edges: Mapped["StepTemplateEdge"] = relationship(
        "StepTemplateEdge",
        primaryjoin=id == StepTemplateEdge.process_template_id,
        cascade="all, delete-orphan",
    )
    resource_slots: Mapped[List["ResourceSlot"]] = relationship(
        "ResourceSlot", back_populates="process_template"
    )


class ResourceSlot(Base):
    __tablename__ = "resource_slot"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column()
    process_template_id: Mapped[UUID] = mapped_column(
        ForeignKey("process_template.id"), nullable=False
    )
    process_template: Mapped[ProcessTemplate] = relationship(
        ProcessTemplate, back_populates="resource_slots"
    )
    resource_type_id: Mapped[UUID] = mapped_column(
        ForeignKey("resource_type.id"), nullable=False
    )
    resource_type: Mapped[UUID] = relationship("ResourceType")
    direction: Mapped[Direction] = mapped_column(
        Enum(Direction, name="direction_enum"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "process_template_id", "name", name="uq_process_template_name"
        ),
    )


class ProcessRun(Base):
    __tablename__ = "process_run"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    name: Mapped[str] = mapped_column(unique=True, nullable=False)
    description: Mapped[str] = mapped_column(unique=False, nullable=False)

    process_template_id: Mapped[UUID] = mapped_column(
        ForeignKey("process_template.id"), nullable=False
    )
    template: Mapped[ProcessTemplate] = relationship()

    assignments: Mapped[list["ResourceAssignment"]] = relationship(
        "ResourceAssignment", back_populates="process_run", cascade="all, delete-orphan"
    )
    resources = associationproxy.association_proxy(
        "assignments",
        "resource",
        creator=lambda res_slot: ResourceAssignment(
            resource=res_slot[0], resource_slot=res_slot[1]
        ),
    )

    steps: Mapped[List["Step"]] = relationship(back_populates="process_run")

    def __init__(self, *args, **kwargs):
        template: Optional[ProcessTemplate] = kwargs.get("template", None)
        if not template:
            return
        super().__init__(*args, **kwargs)
        for step_template in template.step_templates:
            Step(process_run=self, template=step_template)

    @validates("resources")
    def _check_resource(self, key, resource):
        # 1) type must match one of the slots
        acceptable_slots = {
            slot.id
            for slot in self.template.resource_slots
            if slot.resource_type_id == resource.resource_type_id
        }
        if resource.resource_slot_id not in acceptable_slots:
            raise ValueError(f"Resource {resource.id} has wrong type/slot")

        # 2) slot must not already be used
        used_slots = {res.resource_slot_id for res in self.resources}
        if resource.resource_slot_id in used_slots:
            raise ValueError(
                f"Slot {resource.resource_slot_id} is already occupied in run {self.id}"
            )

        return resource


class ResourceAssignment(Base):
    __tablename__ = "resource_assignment"
    process_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("process_run.id"), primary_key=True
    )
    resource_slot_id: Mapped[UUID] = mapped_column(
        ForeignKey("resource_slot.id"), primary_key=True
    )
    resource_id: Mapped[UUID] = mapped_column(ForeignKey("resource.id"), nullable=False)

    # ties back to the run
    process_run: Mapped["ProcessRun"] = relationship(
        "ProcessRun"  # , back_populates="assignments"
    )
    # ties back to the slot
    resource_slot: Mapped["ResourceSlot"] = relationship()
    # ties back to the underlying Resource
    resource: Mapped["Resource"] = relationship(
        "Resource"  # , back_populates="assignments"
    )

    # enforce “one assignment per run+slot”
    __table_args__ = (
        UniqueConstraint("process_run_id", "resource_slot_id", name="uq_run_slot"),
    )
