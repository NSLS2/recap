from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from recap.utils.general import CONVERTERS, TARGET_FIELD, make_slug

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

    attribute_group_template_id: Mapped[UUID] = mapped_column(
        ForeignKey("attribute_group_template.id")
    )
    attribute_group_template = relationship(
        AttributeGroupTemplate, back_populates="attribute_templates"
    )
    enum_options: Mapped[list["AttributeEnumOption"]] = relationship(
        "AttributeEnumOption",
        back_populates="attribute_template",
        cascade="all, delete-orphan",
    )


# --- Keep slug always in sync with name ---
@event.listens_for(AttributeTemplate, "before_insert", propagate=True)
def _before_insert(mapper, connection, target: AttributeTemplate):
    target.slug = make_slug(target.name)


@event.listens_for(AttributeTemplate, "before_update", propagate=True)
def _before_update(mapper, connection, target: AttributeTemplate):
    target.slug = make_slug(target.name)


class AttributeEnumOption(TimestampMixin, Base):
    __tablename__ = "attribute_enum_option"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    attribute_template_id: Mapped[UUID] = mapped_column(
        ForeignKey("attribute_template.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attribute_template: Mapped[AttributeTemplate] = relationship(
        AttributeTemplate, back_populates="enum_options"
    )

    value: Mapped[str] = mapped_column(nullable=False)
    label: Mapped[str | None] = mapped_column(nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(
        MutableDict.as_mutable(JSON), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "attribute_template_id",
            "value",
            name="uq_attr_enum_option_value_per_template",
        ),
    )


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
    enum_option_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("attribute_enum_option.id"), nullable=True, index=True
    )
    enum_option: Mapped["AttributeEnumOption"] = relationship()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        value = kwargs.pop("value", self.template.default_value)
        self.set_value(value)

    @validates(
        "int_value",
        "float_value",
        "bool_value",
        "str_value",
        "datetime_value",
        "array_value",
        "enum_option_id",
    )
    def _validate_exclusive_value(self, key, value):
        if value is not None:
            current_type = self.template.value_type if self.template else None
            expected_key = (
                "enum_option_id" if current_type == "enum" else f"{current_type}_value"
            )
            if key != expected_key:
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
            "enum_option_id",
        ):
            setattr(self, f, None)
        vt = self.template.value_type
        if vt == "enum":
            option = self._resolve_enum_option(value)
            self.enum_option = option
            self.enum_option_id = option.id if option else None
            return
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
        if vt == "enum":
            return self.enum_option.value if self.enum_option else None
        return getattr(self, f"{vt}_value", None)

    @value.setter
    def value(self, v):
        self.set_value(v)

    @hybrid_property
    def enum_payload(self) -> dict[str, Any] | None:
        if self.template and self.template.value_type == "enum":
            return self.enum_option.payload if self.enum_option else None
        return None

    def _resolve_enum_option(self, raw: Any) -> "AttributeEnumOption | None":
        """
        Convert user input into an AttributeEnumOption instance belonging to this template.
        Accepts an AttributeEnumOption, UUID, or a value that can be cast to str.
        """
        if raw is None:
            return None

        template = self.template
        if not template or template.value_type != "enum":
            raise ValueError("Enum option set on non-enum attribute")

        options = list(template.enum_options or [])
        if not options:
            raise ValueError(f"No enum options defined for attribute {template.name}")

        if isinstance(raw, AttributeEnumOption):
            if (
                raw.attribute_template_id
                and template.id
                and (raw.attribute_template_id != template.id)
            ):
                raise ValueError("Enum option does not belong to attribute template")
            return raw

        if isinstance(raw, UUID):
            match = next((opt for opt in options if opt.id == raw), None)
            if match:
                return match

        target_value = str(raw)
        match = next((opt for opt in options if opt.value == target_value), None)
        if match:
            return match

        allowed = ", ".join(sorted(opt.value for opt in options))
        raise ValueError(
            f"Invalid enum value {target_value!r} for attribute {template.name}; "
            f"allowed values: {allowed}"
        )
