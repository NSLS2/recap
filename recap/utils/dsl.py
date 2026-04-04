from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, create_model
from sqlalchemy.inspection import inspect
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql import Select

# Imported lazily-ish at module level to avoid circular imports — AttributeValueSchema
# has no dependency on dsl.py so this is safe.
from recap.schemas.attribute import AttributeValueSchema


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
                current = getattr(self, name)
                if isinstance(current, AttributeValueSchema):
                    current.value = value
                    return
                setattr(self, name, value)
                return
        raise KeyError(f"No field with alias '{alias}'")

    def __getitem__(self, alias: str):
        return self.get(alias)

    def __setitem__(self, alias: str, value):
        self.set(alias, value)

    def items(self):
        for name in self.__class__.model_fields:
            yield name, getattr(self, name)

    def keys(self):
        return self.__class__.model_fields.keys()

    def values(self):
        for name in self.__class__.model_fields:
            yield getattr(self, name)


class AliasMixinBase(AliasMixin, BaseModel):
    """Concrete Pydantic base class that combines :class:`AliasMixin` with
    :class:`~pydantic.BaseModel`.

    Use this as the ``__base__`` for dynamically-created models (via
    ``create_model``) that need both Pydantic validation and the alias
    get/set helpers provided by :class:`AliasMixin`.  Using a single
    concrete class avoids the type-checker error that arises when a bare
    ``(AliasMixin, BaseModel)`` tuple is passed as ``__base__``.
    """


def map_dtype_to_pytype(dtype: str):
    return {
        "float": float,
        "int": int,
        "str": str,
        "bool": bool,
        "datetime": datetime,
        "array": list,
        "enum": str,
    }[dtype]


def lock_instance_fields(model: BaseModel, fields: set[str]) -> BaseModel:
    """
    Return the given model instance with selected fields made read-only.
    We override __setattr__ on the instance to guard critical attributes.
    """
    locked = set(fields)
    original_setattr = model.__setattr__

    def _locked_setattr(self, name, value):
        if name in locked:
            raise TypeError(f"{name} is read-only")
        return original_setattr(name, value)

    object.__setattr__(model, "__setattr__", _locked_setattr.__get__(model))
    return model


def build_param_values_model(group_slug: str, attr_templates):
    from recap.schemas.attribute import AttributeValueSchema

    fields: dict[str, tuple] = {}
    for entry in attr_templates:
        if len(entry) == 4:
            name, slug, value_type, metadata = entry
            unit = None
        else:
            name, slug, value_type, metadata, unit = entry
        pytype = map_dtype_to_pytype(value_type)
        meta = metadata or {}
        ge = meta.get("min") if value_type in {"int", "float"} else None
        le = meta.get("max") if value_type in {"int", "float"} else None
        value_model = create_model(
            f"{group_slug}_{slug}_value",
            __base__=AttributeValueSchema,
            value=(pytype | None, Field(default=None, ge=ge, le=le)),
            unit=(str | None, Field(default=unit)),
        )
        fields[slug] = (
            value_model,
            Field(default_factory=value_model, alias=name),
        )

    return create_model(
        f"{group_slug}_values",
        **fields,
        __base__=AliasMixinBase,
        __config__=ConfigDict(validate_assignment=True, populate_by_name=True),
    )
