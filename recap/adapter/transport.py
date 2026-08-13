from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import date, datetime
from enum import Enum
from typing import Any, get_args
from uuid import UUID

from pydantic import BaseModel

from recap.dsl.query import FieldOrdering, FieldPredicate, QuerySpec
from recap.schemas.attribute import AttributeGroupTemplateSchema
from recap.schemas.process import ProcessRunSchema
from recap.schemas.resource import ResourceSchema

_SCALAR_TAG = "__recap_transport_scalar_v1__"


class QueryRequest(BaseModel):
    schema_name: str
    namespace_path: str
    spec: dict[str, Any]

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
            schema_name=schema.__name__,
            namespace_path=namespace_path,
            spec=serialized_spec,
        )


class QueryResult(BaseModel):
    schema_name: str
    items: list[dict[str, Any]]


def serialize_model(model: BaseModel) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    values = model.__dict__
    for name, field in type(model).model_fields.items():
        if name not in values:
            continue
        key = field.serialization_alias or field.alias or name
        payload[key] = _serialize_value(
            values[name], tag_scalars=_contains_any(field.annotation)
        )

    if isinstance(model, ResourceSchema | ProcessRunSchema):
        private = model.__pydantic_private__ or {}
        payload["__recap__"] = {
            "loaded_relations": dict(private.get("_loaded_relations", {})),
            "on_unloaded": private.get("_on_unloaded", "warn"),
        }
    return payload


def _contains_any(annotation: Any) -> bool:
    return annotation is Any or any(_contains_any(arg) for arg in get_args(annotation))


def _serialize_value(value: Any, *, tag_scalars: bool = False) -> Any:
    if isinstance(value, BaseModel):
        return serialize_model(value)
    if isinstance(value, Mapping):
        return {
            str(key): _serialize_value(item, tag_scalars=tag_scalars)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_serialize_value(item, tag_scalars=tag_scalars) for item in value]
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
    if isinstance(value, ResourceSchema | ProcessRunSchema) and isinstance(
        metadata, Mapping
    ):
        value.set_loaded_relations(
            dict(metadata.get("loaded_relations", {})),
            on_unloaded=metadata.get("on_unloaded", "warn"),
        )

    values = value.__dict__
    for name, field in type(value).model_fields.items():
        if name not in values:
            continue
        key = field.validation_alias or field.alias or name
        if not isinstance(key, str) or key not in payload:
            key = name
        if key in payload:
            _restore_metadata(values[name], payload[key])
