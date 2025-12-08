from datetime import datetime
from functools import lru_cache

from pydantic import BaseModel, ConfigDict, Field, create_model
from sqlalchemy.inspection import inspect
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql import Select


def resolve_path(
    base_model,
    stmt: Select,
    path: tuple[str, ...],
    joined_paths: dict[tuple[str, ...], type],
) -> tuple[Select, InstrumentedAttribute]:
    """
    Walk a path like ("campaign", "proposal") from base_model using SQLAlchemy
    inspection. Along the way, join relationships as needed.

    Returns:
        stmt: the possibly-modified Select with joins applied
        attr: the final attribute (typically a column) on the last entity
    """
    if not path:
        raise ValueError("Empty path")

    current_entity = base_model
    mapper = inspect(current_entity)

    *rel_path, field_name = path

    # Walk relationships (if any)
    for depth, rel_name in enumerate(rel_path):
        subpath = tuple(rel_path[: depth + 1])

        if subpath in joined_paths:
            current_entity = joined_paths[subpath]
            mapper = inspect(current_entity)
            continue

        rel = mapper.relationships.get(rel_name)
        if rel is None:
            raise ValueError(
                f"{current_entity.__name__} has no relationship '{rel_name}' "
                f"(path: {'__'.join(path)})"
            )

        rel_attr: InstrumentedAttribute = getattr(current_entity, rel.key)
        target_entity = rel.mapper.class_

        # Apply the join
        stmt = stmt.join(rel_attr)

        joined_paths[subpath] = target_entity
        current_entity = target_entity
        mapper = inspect(current_entity)

    # Now resolve the final field on the last entity
    try:
        attr = getattr(current_entity, field_name)
    except AttributeError as err:
        raise ValueError(
            f"{current_entity.__name__} has no attribute '{field_name}' "
            f"(path: {'__'.join(path)})"
        ) from err

    if not isinstance(attr, InstrumentedAttribute):
        raise ValueError(
            f"Attribute '{field_name}' on {current_entity.__name__} is not a column/relationship "
            f"(path: {'__'.join(path)})"
        )

    return stmt, attr


class AliasMixin:
    """
    This is a pydantic model mixin that allows a user to access a
    field via its name in the database. For e.g. if a paramter group is
    called "Sample Temperature", its pydantic field is converted to sample_temperature
    But if the user wants to use the original string. This can be used by:
    parameter.get("Sample Temperature") or
    parameter.get("sample_temperature") if they want to use the slugified string

    """

    def get(self, alias: str):
        for name, field in self.__class__.model_fields.items():
            if alias in (field.alias, name):
                return getattr(self, name)
        raise KeyError(f"No field with alias '{alias}'")

    def set(self, alias: str, value):
        for name, field in self.__class__.model_fields.items():
            if alias in (field.alias, name):
                setattr(self, name, value)
                return
        raise KeyError(f"No field with alias '{alias}'")


def map_dtype_to_pytype(dtype: str):
    return {
        "float": float,
        "int": int,
        "str": str,
        "bool": bool,
        "datetime": datetime,
        "array": list,
    }[dtype]


@lru_cache(maxsize=256)
def build_param_values_model(group_slug: str, attr_templates):
    fields: dict[str, tuple] = {}
    for name, slug, value_type in attr_templates:
        pytype = map_dtype_to_pytype(value_type)
        fields[slug] = (pytype | None, Field(default=None, alias=name))

    return create_model(
        f"{group_slug}_values",
        **fields,
        __base__=(AliasMixin, BaseModel),
        __config__=ConfigDict(validate_assignment=True, populate_by_name=True),
    )
