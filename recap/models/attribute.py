from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import sqlalchemy
from sqlalchemy import JSON, Column, DateTime, ForeignKey, Table, event, func
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from recap.utils.general import CONVERTERS, TARGET_FIELD, make_slug

if TYPE_CHECKING:
    from recap.models.resource import ResourceTemplate
    from recap.models.step import StepTemplate

from .base import Base

resource_template_attribute_association = Table(
    "resource_template_attribute_association",
    Base.metadata,
    Column("resource_template_id", sqlalchemy.UUID, ForeignKey("resource_template.id")),
    Column(
        "attribute_template_id", sqlalchemy.UUID, ForeignKey("attribute_template.id")
    ),
)

step_template_attribute_association = Table(
    "step_template_parameter_template_association",
    Base.metadata,
    Column("step_template_id", sqlalchemy.UUID, ForeignKey("step_template.id")),
    Column(
        "attribute_template_id", sqlalchemy.UUID, ForeignKey("attribute_template.id")
    ),
)


class AttributeTemplate(Base):
    __tablename__ = "attribute_template"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(nullable=False)
    slug: Mapped[str | None] = mapped_column(nullable=True)
    value_templates: Mapped[list["AttributeValueTemplate"]] = relationship(
        back_populates="attribute_template",
    )

    resource_templates: Mapped[list["ResourceTemplate"]] = relationship(
        "ResourceTemplate",
        back_populates="attribute_templates",
        secondary=resource_template_attribute_association,
    )
    step_templates: Mapped[list["StepTemplate"]] = relationship(
        back_populates="attribute_templates",
        secondary=step_template_attribute_association,
    )


# --- Keep slug always in sync with name ---
@event.listens_for(AttributeTemplate, "before_insert", propagate=True)
def _before_insert(mapper, connection, target: AttributeTemplate):
    target.slug = make_slug(target.name)


@event.listens_for(AttributeTemplate, "before_update", propagate=True)
def _before_update(mapper, connection, target: AttributeTemplate):
    target.slug = make_slug(target.name)


class AttributeValueTemplate(Base):
    __tablename__ = "attribute_value_template"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(nullable=False)
    slug: Mapped[str | None] = mapped_column(nullable=True)
    value_type: Mapped[str] = mapped_column(nullable=False)
    unit: Mapped[str | None] = mapped_column(nullable=True)
    default_value: Mapped[str | None] = mapped_column(nullable=True)

    attribute_template_id: Mapped[UUID] = mapped_column(
        ForeignKey("attribute_template.id")
    )
    attribute_template = relationship(
        AttributeTemplate, back_populates="value_templates"
    )


# --- Keep slug always in sync with name ---
@event.listens_for(AttributeValueTemplate, "before_insert", propagate=True)
def _before_insert(mapper, connection, target: AttributeValueTemplate):
    target.slug = make_slug(target.name)


@event.listens_for(AttributeValueTemplate, "before_update", propagate=True)
def _before_update(mapper, connection, target: AttributeValueTemplate):
    target.slug = make_slug(target.name)


class AttributeValue(Base):
    __tablename__ = "attribute_value"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    attribute_value_template_id: Mapped[UUID] = mapped_column(
        ForeignKey("attribute_value_template.id")
    )
    template = relationship(AttributeValueTemplate)

    parameter_id: Mapped[UUID] = mapped_column(
        ForeignKey("parameter.id"), nullable=True
    )
    parameter = relationship("Parameter", back_populates="_values")

    property_id: Mapped[UUID] = mapped_column(ForeignKey("property.id"), nullable=True)
    property = relationship("Property", back_populates="_values")

    # __abstract__ = True

    int_value: Mapped[int | None] = mapped_column(nullable=True)
    float_value: Mapped[float | None] = mapped_column(nullable=True)
    bool_value: Mapped[bool | None] = mapped_column(nullable=True)
    str_value: Mapped[str | None] = mapped_column(nullable=True)
    datetime_value: Mapped[datetime | None] = mapped_column(
        DateTime(), nullable=True, default=func.now()
    )
    array_value: Mapped[list[Any] | None] = mapped_column(
        MutableList.as_mutable(JSON), nullable=True
    )
    # attribute_id: Mapped[UUID] = mapped_column(
    #     ForeignKey("attribute.id"), nullable=False
    # )
    # attribute: Mapped["Attribute"] = relationship("Attribute", back_populates="values")

    # @declared_attr
    # def attribute(cls):
    #     return relationship("Attribute")

    def __init__(self, *args, **kwargs):
        value = kwargs.pop("value", None)
        super().__init__(*args, **kwargs)
        if value is None:
            value = self.template.default_value
        self.set_value(value)

    @validates(
        "int_value",
        "float_value",
        "bool_value",
        "str_value",
        "datetime_value",
        "array_value",
    )
    def _validate_exclusive_value(self, key, value):
        if value is not None:
            current_type = self.template.value_type if self.template else None
            if key != f"{current_type}_value":
                raise ValueError(
                    f"{key} cannot be set for property type {current_type}"
                )
        return value

    def set_value(self, value):
        if not self.parameter and not self.property:
            raise ValueError("Parameter or Property must be set before assigning value")

        for f in (
            "int_value",
            "float_value",
            "bool_value",
            "str_value",
            "datetime_value",
            "array_value",
        ):
            setattr(self, f, None)
        vt = self.template.value_type
        try:
            converter = CONVERTERS[vt]
        except KeyError:
            raise ValueError(
                f"Unsupported property type: {self.template.value_type}"
            ) from None

        converted = converter(value)
        setattr(self, TARGET_FIELD[vt], converted)

    @hybrid_property
    def value(self):
        if not self.template:
            return None

        vt = self.template.value_type
        return getattr(self, f"{vt}_value", None)

    # @value.setter
    # def value(self, v):
    #     self.set_value(v)
    #     if not self.template:
    #         return None

    #     vt = self.template.value_type
    #     return getattr(self, f"{vt}_value", None)

    @value.setter
    def value(self, v):
        self.set_value(v)
