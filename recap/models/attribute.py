from typing import List, Optional, TYPE_CHECKING
from uuid import UUID, uuid4

import sqlalchemy
from sqlalchemy import Column, ForeignKey, Table
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import (
    Mapped,
    declared_attr,
    mapped_column,
    relationship,
    validates,
)

if TYPE_CHECKING:
    from recap.models.resource import ResourceTemplate
    from recap.models.step import StepTemplate

from .base import Base

resource_template_attribute_association = Table(
    "resource_template_attribute_association",
    Base.metadata,
    Column("resource_template_id", sqlalchemy.UUID, ForeignKey("resource_template.id")),
    Column("attribute_id", sqlalchemy.UUID, ForeignKey("attribute.id")),
)

step_template_attribute_association = Table(
    "step_template_parameter_template_association",
    Base.metadata,
    Column("step_template_id", sqlalchemy.UUID, ForeignKey("step_template.id")),
    Column("attribute_id", sqlalchemy.UUID, ForeignKey("attribute.id")),
)


class Attribute(Base):
    """
    Attributes store values that may be associated with properties of an object or
    parameters of an step.

    An attribute consists of a name, value_type, unit of measurement and a default_value.
    For example, A heat step has a parameter of temperature. This can be captured by
    creating a temperature attribute like:

    name: "DegreeCelciusTemperature"
    value_type: "float"
    unit: "degC"
    default_value: "100.0"

    An example of a property is volume of a resource, which can be captured by creating
    a volume attribute:

    name: "uL Volume"
    value_type: "int"
    unit: "uL"
    default_value: "40"
    """

    __tablename__ = "attribute"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)
    ref_name: Mapped[Optional[str]] = mapped_column(nullable=True)
    value_type: Mapped[str] = mapped_column(nullable=False)
    unit: Mapped[Optional[str]] = mapped_column(nullable=True)
    default_value: Mapped[Optional[str]] = mapped_column(nullable=True)

    resource_templates: Mapped[List["ResourceTemplate"]] = relationship(
        back_populates="attributes", secondary=resource_template_attribute_association
    )
    step_templates: Mapped[List["StepTemplate"]] = relationship(
        back_populates="attributes", secondary=step_template_attribute_association
    )


class AttributeValueMixin:
    """
    Mixin to store the actual value described by the attribute
    This is used in Parameters and Properties
    """

    __abstract__ = True

    int_value: Mapped[Optional[int]] = mapped_column(nullable=True)
    float_value: Mapped[Optional[float]] = mapped_column(nullable=True)
    bool_value: Mapped[Optional[bool]] = mapped_column(nullable=True)
    str_value: Mapped[Optional[str]] = mapped_column(nullable=True)

    attribute_id: Mapped[UUID] = mapped_column(
        ForeignKey("attribute.id"), nullable=False
    )
    # attribute: Mapped["Attribute"] = relationship()

    @declared_attr
    def attribute(cls):
        return relationship("Attribute")

    def __init__(self, *args, **kwargs):
        value = kwargs.pop("value", None)
        attribute = kwargs.get("attribute")
        super().__init__(*args, **kwargs)
        if value is None:
            value = attribute.default_value
        self.set_value(value)

    @validates("int_value", "float_value", "bool_value", "str_value")
    def _validate_exclusive_value(self, key, value):
        if value is not None:
            current_type = self.attribute.value_type if self.attribute else None
            if key != f"{current_type}_value":
                raise ValueError(
                    f"{key} cannot be set for property type {current_type}"
                )
        return value

    def set_value(self, value):
        if not self.attribute:
            raise ValueError("attribute must be set before assigning value")

        self.int_value = self.float_value = self.bool_value = self.str_value = None

        if self.attribute.value_type == "int":
            self.int_value = int(value)
        elif self.attribute.value_type == "float":
            self.float_value = float(value)
        elif self.attribute.value_type == "bool":
            if isinstance(value, str):
                s = value.strip().lower()
                if s in ("true", "t", "yes", "1"):
                    value = True
                elif s in ("false", "f", "no", "0"):
                    value = False
            self.bool_value = bool(value)
        elif self.attribute.value_type == "str":
            self.str_value = str(value)
        else:
            raise ValueError(f"Unsupported property type: {self.attribute.value_type}")

    @hybrid_property
    def value(self):
        if not self.attribute:
            return None

        vt = self.attribute.value_type
        return getattr(self, f"{vt}_value", None)

    @value.setter
    def value(self, v):
        self.set_value(v)
        if not self.attribute:
            return None

        vt = self.attribute.value_type
        return getattr(self, f"{vt}_value", None)

    @value.setter
    def value(self, v):
        self.set_value(v)
