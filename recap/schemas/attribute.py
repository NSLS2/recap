"""Pydantic schemas for attribute templates and attribute values.

This module defines the type hierarchy used to describe and store individual
data fields ("attributes") on resources and process steps:

* :class:`AttributeTemplateSchema` — the persisted blueprint for one attribute
  (name, type, default, unit, optional validation metadata).
* :class:`AttributeGroupTemplateSchema` — a named collection of
  :class:`AttributeTemplateSchema` instances that forms one "property group".
* :class:`AttributeValueSchema` — a single stored value (+ optional unit)
  that corresponds to one :class:`AttributeTemplateSchema`.
* :class:`AttributeTemplateValidator` — a transient Pydantic model used
  during builder operations to validate and coerce a raw attribute definition
  supplied by the user before it is written to the database.
"""

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from recap.schemas.common import CommonFields
from recap.utils.general import CONVERTERS

TypeName = Literal["int", "float", "bool", "str", "datetime", "array", "enum"]


class AttributeTemplateSchema(CommonFields):
    """Persisted blueprint for a single typed attribute.

    An :class:`AttributeTemplateSchema` belongs to an
    :class:`AttributeGroupTemplateSchema` and defines the name, type, default
    value, optional unit, and optional validation metadata for one data field.

    Attributes:
        name: Human-readable attribute name (may contain spaces or mixed case).
        slug: Snake_case identifier derived from *name*, used for Python
            attribute access (e.g. ``prop.values.catalog_id``).
        value_type: One of the :data:`~recap.schemas.attribute.TypeName`
            literals — ``"int"``, ``"float"``, ``"bool"``, ``"str"``,
            ``"datetime"``, ``"array"``, or ``"enum"``.
        unit: Physical unit string (e.g. ``"uL"``), or ``None`` when
            the attribute is dimensionless.
        default_value: The value stored when a resource is instantiated
            without an explicit override.
        metadata: Optional dict carrying extra validation hints:

            - ``min`` / ``max`` — numeric bounds for ``int`` and ``float``.
            - ``choices`` — list of allowed strings for ``enum``.
    """

    name: str
    slug: str
    value_type: TypeName
    unit: str | None
    default_value: Any
    metadata: dict[str, Any] | None = Field(default_factory=dict, alias="metadata_json")


class AttributeGroupRef(CommonFields):
    """Lightweight reference to an attribute group, containing only identity fields.

    Used in contexts where the full
    :class:`AttributeGroupTemplateSchema` is not needed — for example, when
    serialising a parent relationship without repeating all attribute details.

    Attributes:
        name: Human-readable name of the group.
        slug: Snake_case identifier of the group.
    """

    name: str
    slug: str


class AttributeGroupTemplateSchema(CommonFields):
    """A named group of :class:`AttributeTemplateSchema` instances.

    Property groups organise related attributes together.  For example, a
    ``"content"`` group on a well resource might contain ``smiles``,
    ``catalog_id``, and ``volume``.

    Attributes:
        name: Human-readable group name.
        slug: Snake_case identifier; used as the Python attribute on the
            ``properties`` object (e.g. ``resource.properties.content``).
        attribute_templates: Ordered list of attribute blueprints belonging
            to this group.
    """

    name: str
    slug: str
    attribute_templates: list[AttributeTemplateSchema]


class AttributeValueSchema(BaseModel):
    """A single stored attribute value with an optional physical unit.

    Instances of this class are what you read and write when accessing
    resource properties or step parameters at runtime::

        well.properties.content.volume.value   # 10.0
        well.properties.content.volume.unit    # "uL"
        str(well.properties.content.volume)    # "10.0uL"

        # Mutate the value in-place (unit is preserved)
        well.properties.content.volume = 8.5

    Attributes:
        value: The stored scalar value.  Type is validated against the
            corresponding :class:`AttributeTemplateSchema` at write time.
        unit: Physical unit string, or ``None`` for dimensionless attributes.
            Preserved across value assignments.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    value: Any = None
    unit: str | None = None

    def __str__(self) -> str:
        """Return ``"<value><unit>"`` when a unit is set, otherwise ``str(value)``."""
        if self.unit:
            return f"{self.value}{self.unit}"
        return str(self.value)

    @model_validator(mode="before")
    @classmethod
    def coerce_scalar_value(cls, data):
        """Allow constructing an :class:`AttributeValueSchema` from a bare scalar.

        When *data* is not a ``dict`` (e.g. ``AttributeValueSchema(42)``), it
        is wrapped as ``{"value": data}`` so the scalar becomes the ``.value``
        field.  Dict inputs pass through unchanged.
        """
        if isinstance(data, dict):
            return data
        return {"value": data}


class AttributeTemplateValidator(BaseModel):
    """Transient validator for a raw attribute definition supplied via the builder API.

    This model is used **only** during builder operations to validate and
    coerce the dict-style attribute definitions that users pass to
    ``add_properties()`` and similar methods before they are written to the
    database.  It is not persisted.

    Example input accepted by ``add_properties()``::

        {"name": "volume", "type": "float", "default": 10.0, "unit": "uL",
         "metadata": {"min": 0, "max": 20.0}}

    Attributes:
        name: Attribute name (may contain spaces).
        type: One of the :data:`TypeName` literals.
        unit: Physical unit string.  Defaults to an empty string.
        default: Default value, coerced to the Python type for *type* by
            :meth:`coerce_default`.
        metadata: Optional validation hints (``min``, ``max``, ``choices``).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    name: str
    type: TypeName
    unit: str | None = ""
    default: Any = Field(default=None)
    metadata: dict[str, Any] | None = Field(default_factory=dict)

    @field_validator("default")
    @classmethod
    def coerce_default(cls, v: Any, info: ValidationInfo) -> Any:
        """Coerce *default* to the Python type declared by *type*.

        Looks up a converter function for the declared type and applies it to
        *v*.  For ``enum`` types the result is additionally cast to ``str``.

        Raises:
            ValueError: If ``type`` is not yet resolved, is unsupported, or
                if *v* cannot be coerced.
        """
        t = info.data.get("type")
        if t is None:
            raise ValueError("`type` must be provided before `default`")
        conv = CONVERTERS.get(t)
        if conv is None:
            raise ValueError(f"Unsupported type: {t!r}")
        try:
            coerced = conv(v)
        except Exception as e:
            raise ValueError(f"`default` not coercible to {t}: {e}") from e
        if t == "enum":
            coerced = str(coerced)
        return coerced

    @model_validator(mode="after")
    def enforce_enum_choices(self) -> "AttributeTemplateValidator":
        """Ensure ``enum`` attributes declare choices and that *default* is valid.

        Raises:
            ValueError: If ``type`` is ``"enum"`` and ``metadata.choices`` is
                absent, empty, or does not contain the declared *default*.
        """
        if self.type != "enum":
            return self

        choices = (self.metadata or {}).get("choices")
        if not choices:
            raise ValueError("enum attributes require metadata.choices to be set")
        if self.default is not None and str(self.default) not in choices:
            raise ValueError(
                f"default must be one of {', '.join(choices)} (got {self.default})"
            )
        return self
