from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, UniqueConstraint, event, func, inspect, select
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.orm import (
    Mapped,
    attribute_mapped_collection,
    mapped_collection,
    mapped_column,
    relationship,
)

from recap.db.attribute import AttributeGroupTemplate, AttributeValue
from recap.db.base import Base, TimestampMixin
from recap.schemas.common import StepStatus

if TYPE_CHECKING:
    from recap.db.process import ProcessRun, ResourceAssignment, ResourceSlot


def _reject_new(key, _value):
    raise KeyError(
        f"{key!r} is not a valid AttributeValue for this Parameter -"
        "keys are fixed by the template"
    )


class Parameter(TimestampMixin, Base):  # , AttributeValueMixin):
    __tablename__ = "parameter"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    step_id: Mapped[UUID] = mapped_column(ForeignKey("step.id"), nullable=False)
    step: Mapped["Step"] = relationship(back_populates="parameters")

    attribute_group_template_id: Mapped[UUID] = mapped_column(
        ForeignKey("attribute_group_template.id")
    )
    template: Mapped[AttributeGroupTemplate] = relationship(AttributeGroupTemplate)

    _values = relationship(
        "AttributeValue",
        collection_class=mapped_collection(lambda av: av.template.name),
        back_populates="parameter",
        cascade="all, delete-orphan",
    )

    values = association_proxy(
        "_values",
        "value",
        creator=_reject_new,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for value_template in self.template.attribute_templates:
            av = AttributeValue(template=value_template, parameter=self)
            av.set_value(value_template.default_value)


class StepTemplate(TimestampMixin, Base):
    __tablename__ = "step_template"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(nullable=False)
    attribute_group_templates: Mapped[list["AttributeGroupTemplate"]] = relationship(
        "AttributeGroupTemplate",
        back_populates="step_template",
        cascade="all, delete-orphan",
    )
    process_template_id: Mapped[UUID] = mapped_column(
        ForeignKey("process_template.id"),
        nullable=False,
        index=True,
    )
    process_template = relationship("ProcessTemplate", back_populates="step_templates")
    bindings: Mapped[dict[str, "StepTemplateResourceSlotBinding"]] = relationship(
        "StepTemplateResourceSlotBinding",
        back_populates="step_template",
        cascade="all, delete-orphan",
        collection_class=attribute_mapped_collection("role"),
    )
    resource_slots = association_proxy(
        "bindings",
        "resource_slot",
        creator=lambda slot_role, resource_slot: StepTemplateResourceSlotBinding(
            role=slot_role, resource_slot=resource_slot
        ),
    )
    __table_args__ = (
        UniqueConstraint(
            "process_template_id", "name", name="uq_step_name_per_process"
        ),
    )


@event.listens_for(StepTemplate, "before_update", propagate=True)
@event.listens_for(StepTemplate, "before_delete", propagate=True)
def _guard_step_template(mapper, connection, target: StepTemplate):
    # Prevent editing/deleting a step template if runs already exist for the parent process template.
    from recap.db.process import ProcessRun

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
        .where(ProcessRun.process_template_id == target.process_template_id)
    )
    cnt = connection.scalar(count_stmt)
    if cnt and cnt > 0:
        raise ValueError(
            "Cannot modify or delete a process template step when runs exist. "
            "Create a new process template version instead."
        )


class StepTemplateResourceSlotBinding(TimestampMixin, Base):
    __tablename__ = "step_template_resource_slot_binding"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    step_template_id: Mapped[UUID] = mapped_column(
        ForeignKey("step_template.id"), nullable=False
    )
    step_template: Mapped[StepTemplate] = relationship(back_populates="bindings")

    resource_slot_id: Mapped[UUID] = mapped_column(
        ForeignKey("resource_slot.id"), nullable=False
    )
    resource_slot: Mapped["ResourceSlot"] = relationship()

    role: Mapped[str] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("step_template_id", "role", name="uq_step_template_role"),
    )


class StepTemplateEdge(TimestampMixin, Base):
    __tablename__ = "step_template_edge"
    process_template_id: Mapped[UUID] = mapped_column(
        ForeignKey("process_template.id"), primary_key=True
    )
    from_id: Mapped[UUID] = mapped_column(
        ForeignKey("step_template.id"), primary_key=True
    )
    to_id: Mapped[UUID] = mapped_column(
        ForeignKey("step_template.id"), primary_key=True
    )


class StepEdge(TimestampMixin, Base):
    __tablename__ = "step_edge"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    process_id: Mapped[UUID] = mapped_column(
        ForeignKey("process_run.id"), nullable=False
    )
    from_step_id: Mapped[UUID] = mapped_column(ForeignKey("step.id"), nullable=False)
    to_step_id: Mapped[UUID] = mapped_column(ForeignKey("step.id"), nullable=False)


class Step(TimestampMixin, Base):
    __tablename__ = "step"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(nullable=False)

    process_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("process_run.id"), nullable=False
    )
    process_run: Mapped["ProcessRun"] = relationship(back_populates="steps")

    step_template_id: Mapped[UUID] = mapped_column(
        ForeignKey("step_template.id"), nullable=False
    )
    template: Mapped["StepTemplate"] = relationship()
    # parameters: Mapped[List["Parameter"]] = relationship(back_populates="step")
    parameters = relationship(
        "Parameter",
        collection_class=mapped_collection(lambda p: p.template.name),
        back_populates="step",
        cascade="all, delete-orphan",
    )

    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )
    state: Mapped[StepStatus] = mapped_column(
        default=StepStatus.PENDING, nullable=False
    )
    parent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("step.id"), nullable=True, index=True
    )
    parent: Mapped["Step | None"] = relationship(
        "Step", back_populates="children", remote_side=[id]
    )
    children: Mapped[list["Step"]] = relationship(
        "Step", back_populates="parent", cascade="all, delete-orphan"
    )
    assignments: Mapped[dict[UUID, "ResourceAssignment"]] = relationship(
        "ResourceAssignment",
        back_populates="step",
        cascade="all, delete-orphan",
        collection_class=attribute_mapped_collection("resource_slot_id"),
        primaryjoin="Step.id==ResourceAssignment.step_id",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        template: StepTemplate | None = kwargs.get("template")
        if not template:
            return
        # If no name specified use the templates name
        if self.name is None:
            self.name = template.name

        self._initialize_from_step_type(template)

    def _initialize_from_step_type(self, template: StepTemplate):
        """
        Automatically initialize step from step_type
        - Only add parameters if not present
        """
        for param in self.template.attribute_group_templates:
            if not any(
                p.template.id == param.id for name, p in self.parameters.items()
            ):
                self.parameters[param.name] = Parameter(template=param)

    def is_root(self) -> bool:
        return not self.prev_steps

    def is_leaf(self) -> bool:
        return not self.next_steps

    @property
    def resources(self):
        slot_to_role = {
            b.resource_slot_id: b.role for b in self.template.bindings.values()
        }
        return {
            slot_to_role.get(slot_id, str(slot_id)): assignment.resource
            for slot_id, assignment in self.assignments.items()
        }
