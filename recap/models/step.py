from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.orm import (
    Mapped,
    attribute_mapped_collection,
    mapped_collection,
    mapped_column,
    relationship,
)
from sqlalchemy.sql import func

from recap.models.attribute import (
    AttributeTemplate,
    AttributeValue,
    step_template_attribute_association,
)
from recap.models.base import Base
from recap.schemas.common import StepStatus

if TYPE_CHECKING:
    from recap.models.process import ProcessRun, ResourceSlot


def _reject_new(key, _value):
    raise KeyError(
        f"{key!r} is not a valid AttributeValue for this Parameter -"
        "keys are fixed by the template"
    )


class Parameter(Base):  # , AttributeValueMixin):
    __tablename__ = "parameter"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    step_id: Mapped[UUID] = mapped_column(ForeignKey("step.id"), nullable=False)
    step: Mapped["Step"] = relationship(back_populates="parameters")

    attribute_template_id: Mapped[UUID] = mapped_column(
        ForeignKey("attribute_template.id")
    )
    template: Mapped[AttributeTemplate] = relationship(AttributeTemplate)

    # values: Mapped[List["AttributeValue"]] = relationship("AttributeValue", back_populates="parameter")
    _values = relationship(
        "AttributeValue",
        collection_class=mapped_collection(lambda av: av.template.name),
        back_populates="parameter",
        cascade="all, delete-orphan",
    )

    values = association_proxy(
        "_values",
        "value",
        # creator=lambda key, val: AttributeValue(
        #     template=get_value_template_by_name(key),
        #     value=val,
        # ),
        creator=_reject_new,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for value_template in self.template.value_templates:
            av = AttributeValue(template=value_template, parameter=self)
            av.set_value(value_template.default_value)
        # self.set_value(value)


class StepTemplate(Base):
    __tablename__ = "step_template"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(nullable=False)
    attribute_templates: Mapped[list["AttributeTemplate"]] = relationship(
        back_populates="step_templates", secondary=step_template_attribute_association
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
    # parameters: Mapped[List["Parameter"]] = relationship(back_populates="step")
    parameters = relationship(
        "Parameter",
        collection_class=mapped_collection(lambda p: p.template.name),
        back_populates="step",
        cascade="all, delete-orphan",
    )

    # next_steps: Mapped[list["Step"]] = relationship("Step", secondary="step_edge",
    #                                                 primaryjoin=id==StepEdge.from_step_id,
    #                                                 secondaryjoin=id==StepEdge.to_step_id,
    #                                                 viewonly=True, lazy="selectin")
    # prev_steps: Mapped[list["Step"]] = relationship("Step", secondary="step_edge",
    #                                                 primaryjoin=id==StepEdge.to_step_id,
    #                                                 secondaryjoin=id==StepEdge.from_step_id,
    #                                                 viewonly=True,
    #                                                 lazy="selectin")
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )
    state: Mapped[StepStatus] = mapped_column(
        default=StepStatus.PENDING, nullable=False
    )
    # parameters = association_proxy(
    #     "_parameters",
    #     "parameters",
    #     creator=_reject_new
    # )

    # parent_id: Mapped[Optional[UUID]] = mapped_column(
    #     ForeignKey("step.id"), nullable=True
    # )
    # parent: Mapped["Step"] = relationship(
    #     "Step", back_populates="children", remote_side=[id], foreign_keys=[parent_id]
    # )

    # children: Mapped[List["Step"]] = relationship(
    #     "Step", foreign_keys=[parent_id], back_populates="parent"
    # )

    def __init__(self, *args, **kwargs):
        template: StepTemplate | None = kwargs.get("template")
        if not template:
            return
        # If no name specified use the templates name
        if not kwargs.get("name"):
            kwargs["name"] = template.name
        super().__init__(*args, **kwargs)

        self._initialize_from_step_type(template)

    def _initialize_from_step_type(self, template: StepTemplate | None = None):
        """
        Automatically initialize step from step_type
        - Only add parameters if not present
        """
        if not template:
            return

        for param in self.template.attribute_templates:
            if not any(
                p.template.id == param.id for name, p in self.parameters.items()
            ):
                self.parameters[param.name] = Parameter(template=param)

    def is_root(self) -> bool:
        return not self.prev_steps

    def is_leaf(self) -> bool:
        return not self.next_steps


class StepResourceBinding(Base):
    __tablename__ = "step_resource_binding"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    step_id: Mapped[UUID] = mapped_column(ForeignKey("step.id"), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(ForeignKey("resource.id"), nullable=False)
    step_template_resource_slot_binding_id: Mapped[UUID] = mapped_column(
        ForeignKey("step_template_resource_slot_binding.id"), nullable=False
    )
