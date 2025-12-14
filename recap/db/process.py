import typing
from collections import namedtuple
from uuid import UUID, uuid4

from sqlalchemy import Enum, ForeignKey, UniqueConstraint, event, func, inspect, select
from sqlalchemy.ext import associationproxy
from sqlalchemy.orm import (
    Mapped,
    attribute_mapped_collection,
    mapped_collection,
    mapped_column,
    object_session,
    relationship,
    validates,
)

from recap.db.campaign import Campaign
from recap.db.step import Step, StepTemplate, StepTemplateEdge
from recap.exceptions import DuplicateResourceError
from recap.utils.general import Direction

from .base import Base, TimestampMixin

if typing.TYPE_CHECKING:
    from recap.db.resource import Resource, ResourceType

AssignedResource = namedtuple("AssignedResource", ["slot", "resource"])


class ProcessTemplate(TimestampMixin, Base):
    __tablename__ = "process_template"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)
    version: Mapped[str] = mapped_column(nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=False)
    step_templates: Mapped[dict[str, "StepTemplate"]] = relationship(
        back_populates="process_template",
        collection_class=mapped_collection(lambda st: st.name),
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
    steps: Mapped[dict[str, "Step"]] = relationship(
        back_populates="process_run",
        collection_class=mapped_collection(lambda s: s.name),
    )
    campaign_id: Mapped[UUID] = mapped_column(ForeignKey("campaign.id"), nullable=False)
    campaign: Mapped[Campaign] = relationship("Campaign", back_populates="process_runs")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        template: ProcessTemplate | None = kwargs.get("template")
        if template is None:
            raise ValueError("Missing template for ProcessRun")
        for step_template in template.step_templates.values():
            step = Step(template=step_template, name=step_template.name)
            # Attach via the mapped collection to ensure the dict key is derived
            # from the final name instead of a default/None during construction.
            self.steps[step.name] = step

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


@event.listens_for(ProcessTemplate, "before_update", propagate=True)
@event.listens_for(ProcessTemplate, "before_delete", propagate=True)
def _guard_process_template(mapper, connection, target: ProcessTemplate):
    state = inspect(target)
    column_changes = [
        col.key
        for col in mapper.column_attrs
        if state.attrs[col.key].history.has_changes()
        and col.key not in {"modified_date"}
    ]
    if not column_changes:
        return
    count_stmt = (
        select(func.count())
        .select_from(ProcessRun)
        .where(ProcessRun.process_template_id == target.id)
    )
    cnt = connection.scalar(count_stmt)
    if cnt and cnt > 0:
        raise ValueError(
            "Cannot modify or delete a process template with existing runs. "
            "Create a new template version instead."
        )


class ResourceAssignment(TimestampMixin, Base):
    __tablename__ = "resource_assignment"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    process_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("process_run.id"), nullable=False
    )
    resource_slot_id: Mapped[UUID] = mapped_column(
        ForeignKey("resource_slot.id"), nullable=False
    )
    step_id: Mapped[UUID | None] = mapped_column(ForeignKey("step.id"), nullable=True)
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
                    and assignment.resource.parent_id == resource.parent_id
                    and assignment.step_id == self.step_id
                ):
                    raise DuplicateResourceError(
                        resource.name,
                        assignment.process_run.campaign.name,
                        assignment.process_run.name,
                        assignment.step.name if assignment.step else None,
                    )
        return resource

    __table_args__ = (
        UniqueConstraint(
            "process_run_id", "resource_slot_id", "step_id", name="uq_run_slot_step"
        ),
    )
