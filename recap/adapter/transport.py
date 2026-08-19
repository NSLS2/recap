from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import date, datetime
from enum import Enum
from typing import Any, Literal, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from recap.adapter.schema_registry import (
    LEGACY_SCHEMA_KEYS,
    LEGACY_SCHEMAS_BY_NAME,
    SCHEMA_ENTITY_KEYS,
    SCHEMA_PROJECTIONS,
    SCHEMAS_BY_NAME,
    schema_for,
)
from recap.dsl.query import FieldOrdering, FieldPredicate, QuerySpec
from recap.exceptions import RecapProtocolError
from recap.schemas.attribute import AttributeGroupTemplateSchema
from recap.schemas.common import LoadAware

_SCALAR_TAG = "__recap_transport_scalar_v1__"


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity: str
    projection: Literal["full", "ref"]
    namespace_path: str
    spec: dict[str, Any]
    schema_name: str | None = Field(default=None, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_schema_name(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and "entity" not in value and "schema_name" in value:
            schema_name = value["schema_name"]
            schema = SCHEMAS_BY_NAME.get(schema_name) or LEGACY_SCHEMAS_BY_NAME.get(schema_name)
            if schema is not None:
                value = dict(value)
                if schema in SCHEMA_ENTITY_KEYS:
                    value["entity"] = SCHEMA_ENTITY_KEYS[schema]
                    value["projection"] = LEGACY_SCHEMA_KEYS.get(
                        schema_name, (None, SCHEMA_PROJECTIONS[schema])
                    )[1]
                else:
                    value["entity"] = schema_name
                    value["projection"] = "full"
        return value

    @classmethod
    def from_query(
        cls, schema: type[BaseModel], spec: QuerySpec, *, namespace_path: str
    ) -> QueryRequest:
        if not all(isinstance(item, FieldPredicate) for item in spec.predicates):
            raise TypeError("Remote query predicates must use Field(...)")
        if not all(isinstance(item, FieldOrdering) for item in spec.orderings):
            raise TypeError("Remote query orderings must use Field(...)")
        serialized_spec = spec.model_dump(mode="json")
        if not spec.include_mutable:
            serialized_spec.pop("include_mutable", None)
        return cls(
            entity=SCHEMA_ENTITY_KEYS[schema],
            projection=SCHEMA_PROJECTIONS[schema],
            namespace_path=namespace_path,
            spec=serialized_spec,
            schema_name=schema.__name__,
        )

    @property
    def wire_schema(self) -> type[BaseModel]:
        return schema_for(self.entity, self.projection)

    @model_validator(mode="after")
    def validate_entity(self) -> QueryRequest:
        schema_for(self.entity, self.projection)
        return self


class QueryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity: str
    projection: Literal["full", "ref"]
    items: list[dict[str, Any]]
    schema_name: str | None = Field(default=None, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_schema_name(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and "entity" not in value and "schema_name" in value:
            schema_name = value["schema_name"]
            schema = SCHEMAS_BY_NAME.get(schema_name) or LEGACY_SCHEMAS_BY_NAME.get(schema_name)
            if schema is not None:
                value = dict(value)
                if schema in SCHEMA_ENTITY_KEYS:
                    value["entity"] = SCHEMA_ENTITY_KEYS[schema]
                    value["projection"] = LEGACY_SCHEMA_KEYS.get(
                        schema_name, (None, SCHEMA_PROJECTIONS[schema])
                    )[1]
                else:
                    value["entity"] = schema_name
                    value["projection"] = "full"
            elif value["schema_name"] in LEGACY_SCHEMA_KEYS:
                value = dict(value)
                value["entity"], value["projection"] = LEGACY_SCHEMA_KEYS[value["schema_name"]]
            elif value["schema_name"] in LEGACY_SCHEMAS_BY_NAME:
                value = dict(value)
                value["entity"] = value["schema_name"]
                value["projection"] = "full"
        return value

    @model_validator(mode="after")
    def validate_entity(self) -> QueryResult:
        try:
            schema_for(self.entity, self.projection)
        except ValueError:
            if self.schema_name in LEGACY_SCHEMAS_BY_NAME and self.entity == self.schema_name and self.projection == "full":
                return self
            raise
        return self


def serialize_model(
    model: BaseModel, *, _seen: set[int] | None = None
) -> dict[str, Any]:
    if _seen is None:
        _seen = set()
    model_key = id(model)
    repeated = model_key in _seen
    if not repeated:
        _seen.add(model_key)
    payload: dict[str, Any] = {}
    values = model.__dict__
    try:
        relation_fields = getattr(type(model), "_relation_fields", frozenset())
        for name, field in type(model).model_fields.items():
            if name not in values or (repeated and name in relation_fields):
                continue
            key = field.serialization_alias or field.alias or name
            payload[key] = _serialize_value(
                values[name], tag_scalars=_contains_any(field.annotation), _seen=_seen
            )
    finally:
        if not repeated:
            _seen.remove(model_key)

    if isinstance(model, LoadAware):
        private = model.__pydantic_private__ or {}
        payload["__recap__"] = {
            "loaded_relations": dict(private.get("_loaded_relations", {})),
            "on_unloaded": private.get("_on_unloaded", "warn"),
        }
    return payload


def _contains_any(annotation: Any) -> bool:
    return annotation is Any or any(_contains_any(arg) for arg in get_args(annotation))


def _serialize_value(
    value: Any, *, tag_scalars: bool = False, _seen: set[int] | None = None
) -> Any:
    if isinstance(value, BaseModel):
        return serialize_model(value, _seen=_seen)
    if isinstance(value, Mapping):
        return {
            str(key): _serialize_value(item, tag_scalars=tag_scalars, _seen=_seen)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_serialize_value(item, tag_scalars=tag_scalars, _seen=_seen) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if tag_scalars:
            return {_SCALAR_TAG: "datetime", "value": value.isoformat()}
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return _serialize_value(value.value, tag_scalars=tag_scalars)
    return value


def hydrate_result(schema: type[BaseModel], result: QueryResult) -> list[BaseModel]:
    try:
        result_schema = schema_for(result.entity, result.projection)
    except ValueError:
        result_schema = None
    legacy_only_schema = (
        result.schema_name == schema.__name__
        and result.entity == result.schema_name
        and result.projection == "full"
    )
    if result_schema is not schema and not legacy_only_schema:
        raise RecapProtocolError("Query result schema does not match requested schema")
    hydrated: list[BaseModel] = []
    for item in result.items:
        payload = deepcopy(item)
        payload = _decode_tagged_scalars(payload)
        _prepare_dynamic_models(payload)
        model = schema.model_validate(payload)
        _restore_metadata(model, item)
        hydrated.append(model)
    return hydrated


def _decode_tagged_scalars(value: Any) -> Any:
    if isinstance(value, Mapping):
        if (
            len(value) == 2
            and value.get(_SCALAR_TAG) == "datetime"
            and isinstance(value.get("value"), str)
        ):
            return datetime.fromisoformat(value["value"])
        return {key: _decode_tagged_scalars(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_decode_tagged_scalars(item) for item in value]
    return value


def _prepare_dynamic_models(value: Any) -> None:
    if isinstance(value, Mapping):
        template = value.get("template")
        if (
            "values" in value
            and isinstance(template, dict)
            and "attribute_templates" in template
        ):
            value["template"] = AttributeGroupTemplateSchema.model_validate(template)
        for item in value.values():
            _prepare_dynamic_models(item)
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for item in value:
            _prepare_dynamic_models(item)


def _restore_metadata(value: Any, payload: Any) -> None:
    if isinstance(value, BaseModel) and isinstance(payload, Mapping):
        _restore_model_metadata(value, payload)
        return

    if isinstance(value, Mapping) and isinstance(payload, Mapping):
        for key, item in value.items():
            if key in payload:
                _restore_metadata(item, payload[key])
        return

    if isinstance(value, Sequence) and isinstance(payload, Sequence):
        if isinstance(value, str | bytes | bytearray):
            return
        for item, item_payload in zip(value, payload, strict=True):
            _restore_metadata(item, item_payload)


def _restore_model_metadata(value: BaseModel, payload: Mapping[str, Any]) -> None:
    metadata = payload.get("__recap__")
    if isinstance(value, LoadAware) and isinstance(
        metadata, Mapping
    ):
        loaded_relations = dict(metadata.get("loaded_relations", {}))
        on_unloaded = metadata.get("on_unloaded", "warn")
        if loaded_relations or on_unloaded != "warn":
            value.set_loaded_relations(loaded_relations, on_unloaded=on_unloaded)

    values = value.__dict__
    for name, field in type(value).model_fields.items():
        if name not in values:
            continue
        key = field.validation_alias or field.alias or name
        if not isinstance(key, str) or key not in payload:
            key = name
        if key in payload:
            _restore_metadata(values[name], payload[key])
