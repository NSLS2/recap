import enum
import typing
from collections import namedtuple
from uuid import UUID, uuid4

from sqlalchemy import Enum, ForeignKey, UniqueConstraint
from sqlalchemy.ext import associationproxy
from sqlalchemy.orm import (
    Mapped,
    attribute_mapped_collection,
    mapped_column,
    object_session,
    relationship,
    validates,
)

from recap.db.campaign import Campaign
from recap.db.step import Step, StepTemplate, StepTemplateEdge
from recap.exceptions import DuplicateResourceError

from .base import Base, TimestampMixin

if typing.TYPE_CHECKING:
    from recap.db.resource import Resource, ResourceType

AssignedResource = namedtuple("AssignedResource", ["slot", "resource"])


class Direction(str, enum.Enum):
    input = "input"
    output = "output"


class ProcessTemplate(TimestampMixin, Base):
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


class ResourceSlot(TimestampMixin, Base):
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
    resource_type: Mapped["ResourceType"] = relationship("ResourceType")
    direction: Mapped[Direction] = mapped_column(
        Enum(Direction, name="direction_enum"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "process_template_id", "name", name="uq_process_template_name"
        ),
    )


class ProcessRun(TimestampMixin, Base):
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
        primaryjoin="and_(ProcessRun.id==ResourceAssignment.process_run_id, ResourceAssignment.step_id==None)",
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
    campaign_id: Mapped[UUID] = mapped_column(ForeignKey("campaign.id"), nullable=False)
    campaign: Mapped[Campaign] = relationship("Campaign", back_populates="process_runs")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        template: ProcessTemplate | None = kwargs.get("template")
        if template is None:
            raise ValueError("Missing template for ProcessRun")
        for step_template in template.step_templates:
            Step(process_run=self, template=step_template)

    @validates("assignments")
    def _check_assignment(self, key, assignment: "ResourceAssignment"):
        if assignment.process_run is None:
            assignment.process_run = self
        sess = object_session(self)
        if sess is not None and assignment not in sess:
            sess.add(assignment)
        slot = assignment.resource_slot
        resource = assignment.resource

        # Resource must advertise the slot's type via its template's types
        resource_type_ids = {rt.id for rt in resource.template.types}
        if slot.resource_type_id not in resource_type_ids:
            raise ValueError(
                f"Resource {resource.name} does not match required type for slot {slot.name}"
            )

        # Slot must not already be used in this run
        for existing in self.assignments.values():
            if existing is assignment:
                continue
            if existing.resource_slot_id == slot.id:
                raise ValueError(
                    f"Slot {slot.name} is already occupied in run {self.id}"
                )

        return assignment

    @property
    def assigned_resources(self):
        ar = []
        for resource_slot, resource in self.resources.items():
            ar.append(AssignedResource(slot=resource_slot, resource=resource))
        return ar


class ResourceAssignment(TimestampMixin, Base):
    __tablename__ = "resource_assignment"
    process_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("process_run.id"), primary_key=True
    )
    resource_slot_id: Mapped[UUID] = mapped_column(
        ForeignKey("resource_slot.id"), primary_key=True
    )
    step_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("step.id"), primary_key=True
    )
    resource_id: Mapped[UUID] = mapped_column(ForeignKey("resource.id"), nullable=False)

    process_run: Mapped["ProcessRun"] = relationship("ProcessRun")
    resource_slot: Mapped["ResourceSlot"] = relationship()
    resource: Mapped["Resource"] = relationship(
        "Resource", back_populates="assignments"
    )
    step: Mapped["Step | None"] = relationship("Step", back_populates="assignments")

    @validates("resource")
    def _check_resource_campaign_uniqueness(self, key, resource: "Resource"):
        if self.process_run and self.process_run.campaign:
            campaign_id = self.process_run.campaign.id
            for assignment in resource.assignments:
                if assignment is self:
                    continue
                if (
                    assignment.process_run
                    and assignment.process_run.campaign_id == campaign_id
                ):
                    raise DuplicateResourceError(
                        resource.name, self.process_run.campaign.name
                    )

    __table_args__ = (
        UniqueConstraint(
            "process_run_id", "resource_slot_id", "step_id", name="uq_run_slot_step"
        ),
    )
