"""Shared primitive types and base models used across all RECAP schemas.

This module defines the :class:`ValueType` and :class:`StepStatus` enumerations,
the :class:`Attribute` helper model used internally when building attribute
templates, and :class:`CommonFields` which provides the audit fields
(``id``, ``create_date``, ``modified_date``) that every persisted schema
model inherits.
"""

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ValueType(str, Enum):
    """Enumeration of the scalar types supported by attribute templates.

    Each member corresponds to the ``type`` string accepted when declaring a
    property or step parameter via the builder API::

        template_builder.add_properties({
            "dimensions": [
                {"name": "rows", "type": "int", "default": 8},
            ]
        })

    Members:
        INT: 64-bit integer (Python :class:`int`).
        STR: Unicode string (Python :class:`str`).
        BOOL: Boolean flag (Python :class:`bool`).
        FLOAT: Double-precision float (Python :class:`float`).
        DATETIME: Timezone-naive datetime (Python :class:`~datetime.datetime`).
        ARRAY: Ordered list of arbitrary values (Python :class:`list`).
        ENUM: String constrained to a fixed set of choices defined in
            ``metadata.choices``.  Stored as a :class:`str`.
    """

    INT = "int"
    STR = "str"
    BOOL = "bool"
    FLOAT = "float"
    DATETIME = "datetime"
    ARRAY = "array"
    ENUM = "enum"


class StepStatus(str, Enum):
    """Lifecycle state of a :class:`~recap.schemas.step.StepSchema`.

    Members:
        PENDING: The step has been created but work has not yet started.
        IN_PROGRESS: The step is actively being executed.
        COMPLETE: The step has finished successfully.
    """

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"


# Mapping from ValueType enum to the Python type we expect
TYPE_MAP = {
    ValueType.INT: int,
    ValueType.STR: str,
    ValueType.BOOL: bool,
    ValueType.FLOAT: float,
    ValueType.DATETIME: datetime,
    ValueType.ARRAY: list,
    ValueType.ENUM: str,
}

# DefaultValue = Union[int, float, bool, str]
DefaultValue = int | float | bool | str | datetime | list | None


class Attribute(BaseModel):
    """Internal helper that pairs an attribute name/slug with its type and default.

    This model is used during DSL processing to carry the parsed definition of
    a single attribute before it is written to the database as an
    :class:`~recap.schemas.attribute.AttributeTemplateSchema`.  It validates
    that *default_value* is compatible with *value_type* at construction time.

    Attributes:
        name: Human-readable attribute name (may contain spaces).
        slug: URL/identifier-safe version of *name* (snake_case).
        value_type: One of the :class:`ValueType` members.
        default_value: The default value; must be an instance of the Python
            type that corresponds to *value_type*.
    """

    name: str
    slug: str
    value_type: ValueType
    default_value: DefaultValue

    @model_validator(mode="after")
    def check_default_value(self):
        """Validate that *default_value* matches the declared *value_type*.

        Raises:
            ValueError: If ``default_value`` is not an instance of the Python
                type mapped to ``value_type`` in :data:`TYPE_MAP`.
        """
        if not isinstance(self.default_value, TYPE_MAP[self.value_type]):
            raise ValueError(
                f"default_value must be {TYPE_MAP[self.value_type].__name__}",
                f"got {type(self.default_value).__name__} instead.",
            )
        return self


class CommonFields(BaseModel):
    """Base Pydantic model that adds standard audit fields to every persisted schema.

    All schema models that map to a database row inherit from this class.  The
    fields are marked ``repr=False`` to keep ``repr()`` output concise.

    Attributes:
        id: UUID primary key assigned by the database.
        create_date: Timestamp when the record was first created.
        modified_date: Timestamp of the most recent update.
    """

    id: UUID = Field(repr=False)
    create_date: datetime = Field(repr=False)
    modified_date: datetime = Field(repr=False)

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
