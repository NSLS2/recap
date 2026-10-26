from collections.abc import Mapping, Sequence, Set
from copy import deepcopy
from datetime import datetime
from functools import cache, lru_cache
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, create_model
from sqlalchemy.inspection import inspect
from sqlalchemy.orm import InstrumentedAttribute, RelationshipProperty, aliased
from sqlalchemy.sql import Select

# Imported lazily-ish at module level to avoid circular imports — AttributeValueSchema
# has no dependency on dsl.py so this is safe.
from recap.schemas.attribute import AttributeObserver, AttributeValueSchema


def bind_attribute_observers(owner: BaseModel) -> None:
    """Bind fresh observation state to one editable resource or step draft.

    Referenced templates/resources are not traversed. Each nested owner must be
    bound separately so edits never notify an unrelated aggregate.
    """
    domain = "parameters" if "parameters" in type(owner).model_fields else "properties"
    groups = owner.__dict__.get(domain)
    if groups is None:
        return
    setattr(owner, f"_{domain}_dirty", False)
    if isinstance(groups, BaseModel):
        groups = {
            field.alias or name: getattr(groups, name)
            for name, field in type(groups).model_fields.items()
        }
    for group in groups.values():
        group._changed_attributes = {}
        for name, field in type(group.values).model_fields.items():
            value = getattr(group.values, name)
            key = field.alias or name

            def notify(changed_field, *, group=group, key=key):
                group._changed_attributes.setdefault(key, set()).add(changed_field)
                setattr(owner, f"_{domain}_dirty", True)

            value._observer = AttributeObserver(notify)


def mark_attribute_groups_changed(owner: BaseModel) -> None:
    """Force comparison after accepting an externally edited model."""
    domain = "parameters" if "parameters" in type(owner).model_fields else "properties"
    groups = owner.__dict__.get(domain, {})
    groups = (
        groups.values()
        if isinstance(groups, dict)
        else [getattr(groups, name) for name in type(groups).model_fields]
    )
    for group in groups:
        group._changed_attributes = {
            field.alias or name: {"value", "unit", "metadata_json"}
            for name, field in type(group.values).model_fields.items()
        }
    setattr(owner, f"_{domain}_dirty", True)


def attribute_group_payload(group: BaseModel, baseline: dict | None = None) -> dict:
    """Snapshot editable attribute content with conservative mutable fallback.

    Unobserved/replaced values are always read. Bound scalar fields can reuse
    their independent baseline snapshot; dictionaries and lists never do.
    """
    result = {}
    for name, field in type(group.values).model_fields.items():
        key = field.alias or name
        value = getattr(group.values, name)
        previous = baseline.get(key) if baseline is not None else None
        changed = group.changed_attributes.get(key, set())
        observed = value._observer.notify is not None
        result[key] = {
            "value": deepcopy(
                previous["value"]
                if previous is not None
                and observed
                and "value" not in changed
                and not isinstance(value.value, (list, dict, set))
                else value.value
            ),
            "unit": (
                previous["unit"]
                if previous is not None and observed and "unit" not in changed
                else value.unit
            ),
            "metadata_json": deepcopy(value.metadata_json),
        }
    return result


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
    mapper = inspect(current_entity).mapper

    *rel_path, field_name = path

    # Walk relationships (if any)
    for depth, rel_name in enumerate(rel_path):
        subpath = tuple(rel_path[: depth + 1])

        if subpath in joined_paths:
            current_entity = joined_paths[subpath]
            mapper = inspect(current_entity).mapper
            continue

        rel = mapper.relationships.get(rel_name)
        if rel is None:
            raise ValueError(
                f"{mapper.class_.__name__} has no relationship '{rel_name}' "
                f"(path: {'__'.join(path)}). Valid relationships include: {mapper.relationships.keys()}"
            )

        rel_attr: InstrumentedAttribute = getattr(current_entity, rel.key)
        target_entity = aliased(
            rel.mapper.class_, name="field_path_" + "_".join(subpath)
        )

        stmt = stmt.join(target_entity, rel_attr.of_type(target_entity))

        joined_paths[subpath] = target_entity
        current_entity = target_entity
        mapper = inspect(current_entity).mapper

    # Now resolve the final field on the last entity
    try:
        attr = getattr(current_entity, field_name)
    except AttributeError as err:
        raise ValueError(
            f"{mapper.class_.__name__} has no attribute '{field_name}' "
            f"(path: {'__'.join(path)})"
        ) from err

    if not isinstance(attr, InstrumentedAttribute):
        raise ValueError(
            f"Attribute '{field_name}' on {mapper.class_.__name__} is not a column/relationship "
            f"(path: {'__'.join(path)})"
        )

    if isinstance(attr.property, RelationshipProperty):
        raise ValueError(
            f"Attribute '{field_name}' on {mapper.class_.__name__} is a relationship, "
            "not a column"
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


class ReadOnlyError(TypeError):
    """A draft tried to mutate a referenced canonical model."""


class ReadOnlyMixin:
    """Live read-only model view; canonical refreshes remain visible."""

    __slots__ = ()

    def __setattr__(self, name, value):
        target = read_only_unwrap(self)
        raise ReadOnlyError(
            f"{type(target).__name__}.{name} is read-only; edit through its own builder"
        )

    def __delattr__(self, name):
        self.__setattr__(name, None)

    def __getattribute__(self, name):
        value = super().__getattribute__(name)
        if name in type(self).model_fields:
            return _read_only_value(value)
        return value

    def __getattr__(self, name):
        value = super().__getattr__(name)
        return value if name.startswith("_") else _read_only_value(value)

    def __getitem__(self, name):
        return _read_only_value(super().__getitem__(name))

    def __setitem__(self, name, value):
        self.__setattr__(str(name), value)

    def items(self):
        for name, value in read_only_unwrap(self).items():
            yield name, _read_only_value(value)

    @property
    def __pydantic_private__(self):
        return read_only_unwrap(self).__pydantic_private__

    @property
    def __pydantic_fields_set__(self):
        return read_only_unwrap(self).__pydantic_fields_set__

    @property
    def __pydantic_extra__(self):
        return read_only_unwrap(self).__pydantic_extra__

    def __deepcopy__(self, memo):
        return self

    def __copy__(self):
        return self

    def __eq__(self, other):
        return read_only_unwrap(self) == read_only_unwrap(other)

    __hash__ = None

    def model_copy(self, *, update=None, deep=False):
        return read_only_unwrap(self).model_copy(update=update, deep=deep)


@cache
def _read_only_class(cls):
    return type(
        f"ReadOnly{cls.__name__}",
        (ReadOnlyMixin, cls),
        {"__slots__": ("_read_only_target",), "__module__": cls.__module__},
    )


def read_only_view(model: BaseModel) -> BaseModel:
    """Expose a canonical model without copying or globally freezing it."""
    if isinstance(model, ReadOnlyMixin):
        return model
    view = object.__new__(_read_only_class(type(model)))
    object.__setattr__(view, "_read_only_target", model)
    object.__setattr__(view, "__dict__", model.__dict__)
    return view


def read_only_unwrap(value):
    if isinstance(value, ReadOnlyMixin):
        return object.__getattribute__(value, "_read_only_target")
    if isinstance(value, (ReadOnlyMapping, ReadOnlySequence, ReadOnlySet)):
        return value._data
    return value


def _read_only_value(value):
    if isinstance(value, BaseModel):
        return read_only_view(value)
    if isinstance(value, dict):
        return ReadOnlyMapping(value)
    if isinstance(value, (list, tuple)):
        return ReadOnlySequence(value)
    if isinstance(value, set):
        return ReadOnlySet(value)
    return value


class ReadOnlyMapping(Mapping):
    def __init__(self, data):
        self._data = data

    def __getitem__(self, key):
        return _read_only_value(self._data[key])

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)


class ReadOnlySequence(Sequence):
    def __init__(self, data):
        self._data = data

    def __getitem__(self, index):
        value = self._data[index]
        if isinstance(index, slice):
            return ReadOnlySequence(value)
        return _read_only_value(value)

    def __len__(self):
        return len(self._data)

    def __eq__(self, other):
        return self._data == read_only_unwrap(other)

    __hash__ = None


class ReadOnlySet(Set):
    def __init__(self, data):
        self._data = data

    def __contains__(self, value):
        return value in self._data

    def __iter__(self):
        return (_read_only_value(value) for value in self._data)

    def __len__(self):
        return len(self._data)


def _freeze_for_cache(value: Any):
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze_for_cache(v)) for k, v in value.items()))
    if isinstance(value, list | tuple):
        return tuple(_freeze_for_cache(v) for v in value)
    return value


def _thaw_from_cache(value: Any):
    if isinstance(value, tuple):
        if all(
            isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str)
            for item in value
        ):
            return {k: _thaw_from_cache(v) for k, v in value}
        return [_thaw_from_cache(v) for v in value]
    return value


def _normalize_attr_templates(attr_templates):
    normalized = []
    for entry in attr_templates:
        if len(entry) == 4:
            name, slug, value_type, metadata = entry
            unit = None
        else:
            name, slug, value_type, metadata, unit = entry
        normalized.append(
            (
                name,
                slug,
                value_type,
                _freeze_for_cache(metadata or {}),
                unit,
            )
        )
    return tuple(normalized)


@lru_cache(maxsize=4096)
def _build_param_values_model_cached(group_slug: str, normalized_attr_templates):
    fields: dict[str, tuple] = {}
    for name, slug, value_type, metadata, unit in normalized_attr_templates:
        pytype = map_dtype_to_pytype(value_type)
        meta = _thaw_from_cache(metadata) or {}
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


def build_param_values_model(group_slug: str, attr_templates):
    normalized_attr_templates = _normalize_attr_templates(attr_templates)
    return _build_param_values_model_cached(group_slug, normalized_attr_templates)


def _normalize_property_fields(property_fields):
    return tuple(sorted((field_name, alias) for field_name, alias in property_fields))


@lru_cache(maxsize=2048)
def _build_property_groups_model_cached(template_name: str, normalized_property_fields):
    from recap.schemas.resource import PropertySchema

    fields = {
        field_name: (PropertySchema, Field(alias=alias))
        for field_name, alias in normalized_property_fields
    }
    return create_model(
        f"ResourceProperties_{template_name}",
        __base__=AliasMixinBase,
        __config__=ConfigDict(
            validate_assignment=True,
            populate_by_name=True,
            arbitrary_types_allowed=True,
        ),
        **fields,
    )


def build_property_groups_model(template_name: str, property_fields):
    normalized_property_fields = _normalize_property_fields(property_fields)
    return _build_property_groups_model_cached(
        template_name, normalized_property_fields
    )


@lru_cache(maxsize=2048)
def _build_step_parameters_model_cached(
    template_name: str, normalized_parameter_fields
):
    from recap.schemas.step import ParameterSchema

    fields = {
        field_name: (ParameterSchema, Field(alias=alias))
        for field_name, alias in normalized_parameter_fields
    }
    return create_model(
        f"StepParameters_{template_name}",
        __base__=AliasMixinBase,
        __config__=ConfigDict(
            validate_assignment=True,
            populate_by_name=True,
            arbitrary_types_allowed=True,
        ),
        **fields,
    )


def build_step_parameters_model(template_name: str, parameter_fields):
    normalized_parameter_fields = _normalize_property_fields(parameter_fields)
    return _build_step_parameters_model_cached(
        template_name, normalized_parameter_fields
    )
