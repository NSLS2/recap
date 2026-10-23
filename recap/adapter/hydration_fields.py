"""Row projections and model factories, independent of graph assembly."""

from functools import lru_cache
from typing import Any

from pydantic import BaseModel

from recap.schemas.attribute import AttributeGroupTemplateSchema
from recap.schemas.common import SIMPLE_FIELD
from recap.utils.dsl import build_param_values_model

_ABSENT = object()


@lru_cache(maxsize=128)
def _field_mapping(
    schema: type[BaseModel], *, simple_only: bool
) -> tuple[tuple[str, str | None], ...]:
    return tuple(
        (name, field.alias)
        for name, field in schema.model_fields.items()
        if not simple_only or SIMPLE_FIELD in field.metadata
    )


def extract_simple_fields(schema: type[BaseModel], source: Any) -> dict[str, Any]:
    """Project marked scalar fields without touching ORM relationships.

    Preserve the existing trusted-construction preference for field names,
    falling back to ORM aliases only when the field name is absent.
    """
    values = {}
    for name, alias in _field_mapping(schema, simple_only=True):
        value = getattr(source, name, _ABSENT)
        if value is _ABSENT and alias:
            value = getattr(source, alias, _ABSENT)
        if value is not _ABSENT:
            values[name] = value
    return values


def extract_model_fields(schema: type[BaseModel], source: Any) -> dict[str, Any]:
    """Project fields for explicit validation, honoring Pydantic ORM aliases.

    Use this on leaf schemas only: projecting a relationship-bearing schema
    would read those relationships. No validation or construction happens here.
    """
    values = {}
    for name, alias in _field_mapping(schema, simple_only=False):
        value = getattr(source, alias or name, _ABSENT)
        if value is _ABSENT and alias and schema.model_config.get("populate_by_name"):
            value = getattr(source, name, _ABSENT)
        if value is not _ABSENT:
            values[name] = value
    return values


def attribute_values_model(template: AttributeGroupTemplateSchema) -> type[BaseModel]:
    """Reuse the existing cached template-derived value model factory."""
    key = tuple(
        (at.name, at.slug, at.value_type, at.metadata, at.unit)
        for at in template.attribute_templates
    )
    return build_param_values_model(template.slug or template.name, key)
