import enum
from uuid import UUID, uuid4

from sqlalchemy import Enum, ForeignKey, UniqueConstraint
from sqlalchemy.ext import associationproxy
from sqlalchemy.orm import (
    Mapped,
    attribute_mapped_collection,
    mapped_column,
    relationship,
    validates,
)

from recap.models.resource import Resource
from recap.models.step import Step, StepTemplate, StepTemplateEdge

from .base import Base


class Direction(str, enum.Enum):
    input = "input"
    output = "output"


class ProcessTemplate(Base):
    __tablename__ = "process_template"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)
    version: Mapped[str] = mapped_column(nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=False)
    step_templates: Mapped[list["StepTemplate"]] = relationship(
        back_populates="process_template"
    )
    edges: Mapped["StepTemplateEdge"] = relationship(
        "StepTemplateEdge",
        primaryjoin=id == StepTemplateEdge.process_template_id,
        cascade="all, delete-orphan",
    )
    resource_slots: Mapped[list["ResourceSlot"]] = relationship(
        "ResourceSlot", back_populates="process_template"
    )
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_process_template_name_version"),
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

    assignments: Mapped[dict["ResourceSlot", "ResourceAssignment"]] = relationship(
        "ResourceAssignment",
        back_populates="process_run",
        cascade="all, delete-orphan",
        collection_class=attribute_mapped_collection("resource_slot"),
    )
    resources = associationproxy.association_proxy(
        "assignments",
        "resource",
        creator=lambda res_slot, resource: ResourceAssignment(
            resource_slot=res_slot, resource=resource
        ),
    )
    steps: Mapped[list["Step"]] = relationship(back_populates="process_run")

    def __init__(self, *args, **kwargs):
        template: ProcessTemplate | None = kwargs.get("template")
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
        "ProcessRun"
    )  # , back_populates="assignments"
    # ties back to the slot
    resource_slot: Mapped["ResourceSlot"] = relationship()
    # ties back to the underlying Resource
    resource: Mapped["Resource"] = relationship(
        "Resource", back_populates="assignments"
    )

    # enforce “one assignment per run+slot”
    __table_args__ = (
        UniqueConstraint("process_run_id", "resource_slot_id", name="uq_run_slot"),
    )
