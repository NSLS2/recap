from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    UniqueConstraint,
    event,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from recap.utils.general import from_json_value, make_slug, to_json_compatible

if TYPE_CHECKING:
    from recap.db.resource import ResourceTemplate
    from recap.db.step import StepTemplate

from .base import Base, TimestampMixin


class AttributeGroupTemplate(TimestampMixin, Base):
    __tablename__ = "attribute_group_template"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(nullable=False)
    slug: Mapped[str | None] = mapped_column(nullable=True)
    attribute_templates: Mapped[list["AttributeTemplate"]] = relationship(
        back_populates="attribute_group_template",
    )

    resource_template_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("resource_template.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    step_template_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("step_template.id", ondelete="CASCADE"), nullable=True, index=True
    )

    resource_template: Mapped["ResourceTemplate"] = relationship(
        "ResourceTemplate",
        back_populates="attribute_group_templates",
        foreign_keys=resource_template_id,
    )
    step_template: Mapped["StepTemplate"] = relationship(
        "StepTemplate",
        back_populates="attribute_group_templates",
        foreign_keys=step_template_id,
    )

    __table_args__ = (
        # Enforce XOR: exactly one FK must be non-null
        CheckConstraint(
            "(resource_template_id IS NOT NULL) <> (step_template_id IS NOT NULL)",
            name="ck_attr_group_exactly_one_owner",
        ),
        # Keep names unique per owner
        UniqueConstraint(
            "resource_template_id", "name", name="uq_attr_group_name_per_resource"
        ),
        UniqueConstraint(
            "step_template_id", "name", name="uq_attr_group_name_per_step"
        ),
    )


# --- Keep slug always in sync with name ---
@event.listens_for(AttributeGroupTemplate, "before_insert", propagate=True)
def _before_insert(mapper, connection, target: AttributeGroupTemplate):
    target.slug = make_slug(target.name)


@event.listens_for(AttributeGroupTemplate, "before_update", propagate=True)
def _before_update(mapper, connection, target: AttributeGroupTemplate):
    target.slug = make_slug(target.name)


class AttributeTemplate(TimestampMixin, Base):
    __tablename__ = "attribute_template"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(nullable=False)
    slug: Mapped[str | None] = mapped_column(nullable=True)
    value_type: Mapped[str] = mapped_column(nullable=False)
    unit: Mapped[str | None] = mapped_column(nullable=True)
    default_value: Mapped[str | None] = mapped_column(nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON), nullable=True, default=dict
    )

    attribute_group_template_id: Mapped[UUID] = mapped_column(
        ForeignKey("attribute_group_template.id")
    )
    attribute_group_template = relationship(
        AttributeGroupTemplate, back_populates="attribute_templates"
    )


# --- Keep slug always in sync with name ---
@event.listens_for(AttributeTemplate, "before_insert", propagate=True)
def _before_insert(mapper, connection, target: AttributeTemplate):
    target.slug = make_slug(target.name)


@event.listens_for(AttributeTemplate, "before_update", propagate=True)
def _before_update(mapper, connection, target: AttributeTemplate):
    target.slug = make_slug(target.name)


class AttributeValue(TimestampMixin, Base):
    __tablename__ = "attribute_value"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    attribute_template_id: Mapped[UUID] = mapped_column(
        ForeignKey("attribute_template.id")
    )
    template = relationship(AttributeTemplate)

    parameter_id: Mapped[UUID] = mapped_column(
        ForeignKey("parameter.id"), nullable=True
    )
    parameter = relationship("Parameter", back_populates="_values")

    property_id: Mapped[UUID] = mapped_column(ForeignKey("property.id"), nullable=True)
    property = relationship("Property", back_populates="_values")

    value_json: Mapped[Any | None] = mapped_column("value", JSON, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON), nullable=True, default=dict
    )

    def __init__(self, *args, **kwargs):
        raw_value = kwargs.pop("value", None)
        raw_metadata = kwargs.pop("metadata", None)
        super().__init__(*args, **kwargs)
        if self.metadata_json is None:
            self.metadata_json = {}
        if raw_metadata is not None:
            self.metadata_json.update(raw_metadata)
        if raw_value is None and self.template:
            raw_value = self.template.default_value
        if raw_value is not None:
            self.set_value(raw_value)

    def set_value(self, value):
        if not self.parameter and not self.property:
            raise ValueError("Parameter or Property must be set before assigning value")

        vt = self.template.value_type
        if vt == "enum":
            choices = (self.template.metadata_json or {}).get("choices")
            if not choices:
                raise ValueError("enum attributes require metadata.choices to be set")
            if str(value) not in choices:
                raise ValueError(
                    f"{self.template.name} must be one of {', '.join(choices)}"
                )
            value = str(value)
        self.value_json = to_json_compatible(vt, value)

    @hybrid_property
    def value(self):
        if not self.template:
            return None

        vt = self.template.value_type
        return from_json_value(vt, self.value_json)

    @value.setter
    def value(self, v):
        self.set_value(v)
