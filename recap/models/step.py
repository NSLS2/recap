from typing import List, Optional, TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.ext.associationproxy import association_proxy

from recap.models.attribute import (
    Attribute,
    AttributeValueMixin,
    step_template_attribute_association,
)
from recap.models.base import Base

if TYPE_CHECKING:
    from recap.models.process import ProcessRun, ResourceSlot


class Parameter(Base, AttributeValueMixin):
    __tablename__ = "parameter"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    step_id: Mapped[UUID] = mapped_column(ForeignKey("step.id"), nullable=False)
    step: Mapped["Step"] = relationship(back_populates="parameters")

    def __init__(self, *args, **kwargs):
        value = kwargs.pop("value", None)
        attribute = kwargs.get("attribute")
        super().__init__(*args, **kwargs)
        if value is None:
            value = attribute.default_value
        self.set_value(value)


class StepTemplate(Base):
    __tablename__ = "step_template"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)
    attributes: Mapped[List["Attribute"]] = relationship(
        back_populates="step_templates", secondary=step_template_attribute_association
    )

    process_template_id: Mapped[UUID] = mapped_column(
        ForeignKey("process_template.id"), primary_key=True
    )
    process_template = relationship("ProcessTemplate", back_populates="step_templates")

    bindings: Mapped[list["StepTemplateResourceSlotBinding"]] = relationship(
        "StepTemplateResourceSlotBinding",
        back_populates="step_template",
        cascade="all, delete-orphan",
    )
    resource_slots = association_proxy(
        "bindings",
        "resource_slot",
        creator=lambda slot_role: StepTemplateResourceSlotBinding(
            resource_slot=slot_role[0], role=slot_role[1]
        ),
    )
    parent_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("step_template.id"), nullable=True
    )
    parent: Mapped["StepTemplate"] = relationship(
        "StepTemplate",
        back_populates="children",
        remote_side=[id],
        foreign_keys=[parent_id],
    )
    children: Mapped[List["StepTemplate"]] = relationship(
        "StepTemplate", foreign_keys=[parent_id], back_populates="parent"
    )


class StepTemplateResourceSlotBinding(Base):
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


class StepTemplateEdge(Base):
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


class StepEdge(Base):
    __tablename__ = "step_edge"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    process_id: Mapped[UUID] = mapped_column(
        ForeignKey("process_run.id"), nullable=False
    )
    from_step_id: Mapped[UUID] = mapped_column(ForeignKey("step.id"), nullable=False)
    to_step_id: Mapped[UUID] = mapped_column(ForeignKey("step.id"), nullable=False)


class Step(Base):
    __tablename__ = "step"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)

    process_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("process_run.id"), nullable=False
    )
    process_run: Mapped["ProcessRun"] = relationship(back_populates="steps")

    step_template_id: Mapped[UUID] = mapped_column(
        ForeignKey("step_template.id"), nullable=False
    )
    template: Mapped["StepTemplate"] = relationship()
    parameters: Mapped[List["Parameter"]] = relationship(back_populates="step")

    parent_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("step.id"), nullable=True
    )
    parent: Mapped["Step"] = relationship(
        "Step", back_populates="children", remote_side=[id], foreign_keys=[parent_id]
    )

    children: Mapped[List["Step"]] = relationship(
        "Step", foreign_keys=[parent_id], back_populates="parent"
    )

    def __init__(self, *args, **kwargs):
        template: Optional[StepTemplate] = kwargs.get("template", None)
        if not template:
            return
        # If no name specified use the templates name
        if not kwargs.get("name", None):
            kwargs["name"] = template.name
        super().__init__(*args, **kwargs)

        self._initialize_from_step_type(template)

    def _initialize_from_step_type(self, template: Optional[StepTemplate] = None):
        """
        Automatically initialize step from step_type
        - Only add parameters if not present
        """
        if not template:
            return

        for param in self.template.attributes:
            if not any(p.attribute.id == param.id for p in self.parameters):
                self.parameters.append(Parameter(attribute=param, value=None))


class StepResourceBinding(Base):
    __tablename__ = "step_resource_binding"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    step_id: Mapped[UUID] = mapped_column(ForeignKey("step.id"), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(ForeignKey("resource.id"), nullable=False)
    step_template_resource_slot_binding_id: Mapped[UUID] = mapped_column(
        ForeignKey("step_template_resource_slot_binding.id"), nullable=False
    )
