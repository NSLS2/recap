from collections.abc import MutableSequence
from typing import Any, List, Optional, TYPE_CHECKING
from uuid import UUID, uuid4
from datetime import datetime
import sqlalchemy
import json
from sqlalchemy import JSON, Column, ForeignKey, Table, DateTime, func, event
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import (
    Mapped,
    declared_attr,
    mapped_column,
    relationship,
    validates,
)

from recap.utils.general import make_slug

if TYPE_CHECKING:
    from recap.models.resource import ResourceTemplate
    from recap.models.step import StepTemplate

from .base import Base

resource_template_attribute_association = Table(
    "resource_template_attribute_association",
    Base.metadata,
    Column("resource_template_id", sqlalchemy.UUID, ForeignKey("resource_template.id")),
    Column("attribute_template_id", sqlalchemy.UUID, ForeignKey("attribute_template.id")),
)

step_template_attribute_association = Table(
    "step_template_parameter_template_association",
    Base.metadata,
    Column("step_template_id", sqlalchemy.UUID, ForeignKey("step_template.id")),
    Column("attribute_template_id", sqlalchemy.UUID, ForeignKey("attribute_template.id")),
)


def _parse_array_like(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        s = value.strip()
        try:
            loaded_json = json.loads(s)
            if isinstance(loaded_json, list):
                return loaded_json
            return [loaded_json]
        except Exception:
            if "," in s:
                return [part.strip() for part in s.split(",")]
            return [s]
    return [value]


class AttributeTemplate(Base):
    __tablename__ = "attribute_template"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(nullable=False)
    slug: Mapped[Optional[str]] = mapped_column(nullable=True)
    value_templates: Mapped[List["AttributeValueTemplate"]] = relationship(
        back_populates="attribute_template",
    )

    resource_templates: Mapped[List["ResourceTemplate"]] = relationship(
        "ResourceTemplate", back_populates="attribute_templates", secondary=resource_template_attribute_association
    )
    step_templates: Mapped[List["StepTemplate"]] = relationship(
        back_populates="attribute_templates", secondary=step_template_attribute_association
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
    slug: Mapped[Optional[str]] = mapped_column(nullable=True)
    value_type: Mapped[str] = mapped_column(nullable=False)
    unit: Mapped[Optional[str]] = mapped_column(nullable=True)
    default_value: Mapped[Optional[str]] = mapped_column(nullable=True)

    attribute_template_id: Mapped[UUID] = mapped_column(ForeignKey("attribute_template.id"))
    attribute_template = relationship(AttributeTemplate, back_populates="value_templates")


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

    attribute_value_template_id: Mapped[UUID] = mapped_column(ForeignKey("attribute_value_template.id"))
    template = relationship(AttributeValueTemplate)

    parameter_id: Mapped[UUID] = mapped_column(ForeignKey("parameter.id"), nullable=True)
    parameter = relationship("Parameter", back_populates="_values")

    property_id: Mapped[UUID] = mapped_column(ForeignKey("property.id"), nullable=True)
    property = relationship("Property", back_populates="_values")

    # __abstract__ = True

    int_value: Mapped[Optional[int]] = mapped_column(nullable=True)
    float_value: Mapped[Optional[float]] = mapped_column(nullable=True)
    bool_value: Mapped[Optional[bool]] = mapped_column(nullable=True)
    str_value: Mapped[Optional[str]] = mapped_column(nullable=True)
    datetime_value: Mapped[Optional[datetime]] = mapped_column(DateTime(), nullable=True, default=func.now())
    array_value: Mapped[Optional[list[Any]]] = mapped_column(MutableList.as_mutable(JSON), nullable=True)
    # attribute_id: Mapped[UUID] = mapped_column(
    #     ForeignKey("attribute.id"), nullable=False
    # )
    # attribute: Mapped["Attribute"] = relationship("Attribute", back_populates="values")

    # @declared_attr
    # def attribute(cls):
    #     return relationship("Attribute")

    def __init__(self, *args, **kwargs):
        value = kwargs.pop("value", None)
        attribute = kwargs.get("attribute")
        super().__init__(*args, **kwargs)
        if value is None:
            value = self.template.default_value
        self.set_value(value)

    @validates("int_value", "float_value", "bool_value", "str_value", "datetime_value", "array_value")
    def _validate_exclusive_value(self, key, value):
        if value is not None:
            current_type = self.template.value_type if self.template else None
            if key != f"{current_type}_value":
                raise ValueError(f"{key} cannot be set for property type {current_type}")
        return value

    def set_value(self, value):
        if not self.parameter and not self.property:
            raise ValueError("Parameter or Property must be set before assigning value")

        self.int_value = self.float_value = self.bool_value = self.str_value = None
        self.datetime_value = self.array_value = None
        if self.template.value_type == "int":
            self.int_value = int(value)
        elif self.template.value_type == "float":
            self.float_value = float(value)
        elif self.template.value_type == "bool":
            if isinstance(value, str):
                s = value.strip().lower()
                if s in ("true", "t", "yes", "1"):
                    value = True
                elif s in ("false", "f", "no", "0"):
                    value = False
            self.bool_value = bool(value)
        elif self.template.value_type == "str":
            self.str_value = str(value)
        elif self.template.value_type == "datetime":
            if isinstance(value, datetime):
                self.datetime_value = value
            elif isinstance(value, str):
                self.datetime_value = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
            elif isinstance(value, type(None)):
                self.datetime_value = datetime.now()
            else:
                raise ValueError("datetime_value accepts: ISO8601 string or datetime object")
        elif self.template.value_type == "array":
            items = _parse_array_like(value)
            self.array_value = MutableList(items)
        else:
            raise ValueError(f"Unsupported property type: {self.attribute.value_type}")

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
