"""Row projections and model factories, independent of graph assembly."""

from functools import lru_cache
from typing import Any, TypeVar

from pydantic import BaseModel

from recap.schemas.attribute import AttributeGroupTemplateSchema, AttributeValueSchema
from recap.schemas.common import SIMPLE_FIELD
from recap.utils.dsl import build_param_values_model

SchemaT = TypeVar("SchemaT", bound=BaseModel)
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


def validate_fields(schema: type[SchemaT], values: dict[str, Any]) -> SchemaT:
    """Validate extracted data using the schema's existing compiled validator."""
    return schema.model_validate(values)


def construct_model(
    schema: type[SchemaT], values: dict[str, Any], /, **overrides: Any
) -> SchemaT:
    """Construct from trusted data without validation or graph traversal."""
    return schema.model_construct(**(values | overrides))


def attribute_values_model(template: AttributeGroupTemplateSchema) -> type[BaseModel]:
    """Reuse the existing cached template-derived value model factory."""
    key = tuple(
        (at.name, at.slug, at.value_type, at.metadata, at.unit)
        for at in template.attribute_templates
    )
    return build_param_values_model(template.slug or template.name, key)


def extract_attribute_values(
    source: Any, template: AttributeGroupTemplateSchema
) -> dict[str, dict[str, Any]]:
    """Extract stored property/parameter values without creating value models."""
    stored = source._values
    values = {}
    for attribute in template.attribute_templates:
        value = stored.get(attribute.name)
        values[attribute.slug] = {
            "value": value.value if value is not None else None,
            "unit": value.unit if value is not None else attribute.unit,
            "metadata_json": value.metadata_json if value is not None else {},
        }
    return values


def construct_attribute_values(
    template: AttributeGroupTemplateSchema, values: dict[str, dict[str, Any]]
) -> BaseModel:
    """Preserve trusted ORM value construction separately from extraction.

    Existing hydrators use base AttributeValueSchema instances here; public
    dynamic container materialization remains responsible for typed validation.
    """
    return construct_model(
        attribute_values_model(template),
        {
            name: construct_model(AttributeValueSchema, value)
            for name, value in values.items()
        },
    )
