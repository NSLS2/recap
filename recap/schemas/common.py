"""Shared primitive types and base models used across all RECAP schemas.

This module defines the :class:`ValueType` and :class:`StepStatus` enumerations,
the :class:`Attribute` helper model used internally when building attribute
templates, and :class:`CommonFields` which provides the audit fields
(``id``, ``create_date``, ``modified_date``) that every persisted schema
model inherits.
"""

import warnings
from datetime import datetime
from enum import Enum
from typing import Annotated, ClassVar, Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from recap.exceptions import UnloadedFieldError, UnloadedFieldWarning
from recap.lifecycle import LifecycleStatus
from recap.utils.general import make_slug


@runtime_checkable
class LoadAware(Protocol):
    """Protocol for persisted models with selectively loaded relations."""

    def is_loaded(self, relation: str) -> bool: ...

    def require_loaded(self, relation: str) -> None: ...

    def set_loaded_relations(
        self,
        loaded_relations: dict[str, bool],
        *,
        on_unloaded: Literal["silent", "warn", "raise"] = "warn",
    ) -> "LoadAware": ...


class LoadAwareMixin:
    """Track selectively loaded relation fields and guard their access."""

    _relation_fields: ClassVar[frozenset[str]] = frozenset()

    @property
    def _loaded_relations(self) -> dict[str, bool]:
        return (getattr(self, "__pydantic_private__", None) or {}).get("_loaded_relations", {})

    @property
    def _on_unloaded(self) -> Literal["silent", "warn", "raise"]:
        return (getattr(self, "__pydantic_private__", None) or {}).get("_on_unloaded", "warn")

    @property
    def _warned_unloaded(self) -> set[str]:
        return (getattr(self, "__pydantic_private__", None) or {}).setdefault("_warned_unloaded", set())

    def set_loaded_relations(
        self,
        loaded_relations: dict[str, bool],
        *,
        on_unloaded: Literal["silent", "warn", "raise"] = "warn",
    ):
        private = object.__getattribute__(self, "__pydantic_private__")
        if private is None:
            private = {}
            object.__setattr__(self, "__pydantic_private__", private)
        private["_loaded_relations"] = dict(loaded_relations)
        private["_on_unloaded"] = on_unloaded
        private["_warned_unloaded"] = set()
        return self

    def is_loaded(self, relation: str) -> bool:
        private = getattr(self, "__pydantic_private__", None) or {}
        return private.get("_loaded_relations", {}).get(relation, False)

    def require_loaded(self, relation: str) -> None:
        self._handle_unloaded(relation, f"include('{relation}')")

    def _handle_unloaded(self, field_name: str, include_hint: str) -> None:
        private = getattr(self, "__pydantic_private__", None) or {}
        loaded_relations = private.get("_loaded_relations", {})
        if loaded_relations.get(field_name, True):
            return
        message = (
            f"'{field_name}' was not loaded for {type(self).__name__}; "
            f"use {include_hint} or load='eager'."
        )
        on_unloaded = private.get("_on_unloaded", "warn")
        warned = private.setdefault("_warned_unloaded", set())
        if on_unloaded == "raise":
            raise UnloadedFieldError(message)
        if on_unloaded == "warn" and field_name not in warned:
            warnings.warn(message, UnloadedFieldWarning, stacklevel=3)
            warned.add(field_name)

    def __getattribute__(self, name: str):
        relation_fields = object.__getattribute__(self, "_relation_fields")
        if name in relation_fields:
            object.__getattribute__(self, "_handle_unloaded")(name, f"include('{name}')")
        return super().__getattribute__(name)

SIMPLE_FIELD = "simple_field"
NormalizedLabels = Annotated[
    list[str],
    SIMPLE_FIELD,
    BeforeValidator(lambda labels: [make_slug(x) for x in labels]),
]


class ValueType(str, Enum):
    """Enumeration of the scalar types supported by attribute templates.

    Each member corresponds to the ``type`` string accepted when declaring a
    property or step parameter via the builder API::

        template_builder.add_properties({
            "dimensions": [
                {"name": "rows", "type": "int", "default": 8},
            ]
        })

    **Members**

    - ``INT`` — 64-bit integer (Python :class:`int`).
    - ``STR`` — Unicode string (Python :class:`str`).
    - ``BOOL`` — Boolean flag (Python :class:`bool`).
    - ``FLOAT`` — Double-precision float (Python :class:`float`).
    - ``DATETIME`` — Timezone-naive datetime (Python :class:`~datetime.datetime`).
    - ``ARRAY`` — Ordered list of arbitrary values (Python :class:`list`).
    - ``ENUM`` — String constrained to a fixed set of choices defined in
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

    **Members**

    - ``PENDING`` — The step has been created but work has not yet started.
    - ``IN_PROGRESS`` — The step is actively being executed.
    - ``COMPLETE`` — The step has finished successfully.
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

    id: Annotated[UUID, SIMPLE_FIELD] = Field(repr=False)
    create_date: Annotated[datetime, SIMPLE_FIELD] = Field(repr=False)
    modified_date: Annotated[datetime, SIMPLE_FIELD] = Field(repr=False)

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class NamespaceOwnedFields(CommonFields):
    namespace_id: Annotated[UUID, SIMPLE_FIELD]
    status: Annotated[LifecycleStatus, SIMPLE_FIELD]
    revision: Annotated[int, SIMPLE_FIELD]
