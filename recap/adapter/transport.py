"""Flat query envelopes and scalar encoding shared by local and REST batches."""

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from recap.adapter.entity_records import EntityBatch
from recap.adapter.schema_registry import (
    SCHEMA_ENTITY_KEYS,
    SCHEMA_PROJECTIONS,
    schema_for,
)
from recap.dsl.query import FieldOrdering, FieldPredicate, QuerySpec
from recap.exceptions import RecapProtocolError

_SCALAR_TAG = "__recap_transport_scalar_v1__"


class FlatQueryResult(EntityBatch):
    entity: str
    projection: Literal["full", "ref"]

    @model_validator(mode="after")
    def validate_entity(self):
        schema_for(self.entity, self.projection)
        return self


def encode_query_batch(batch: EntityBatch, *, entity: str, projection: str):
    """Tag arbitrary payload scalars without treating dictionaries as references."""
    payload = batch.model_dump()
    for record in payload["entities"]:
        record["fields"] = _encode_scalars(record["fields"])
    return FlatQueryResult(entity=entity, projection=projection, **payload)


def decode_query_batch(schema, result: FlatQueryResult) -> EntityBatch:
    if schema_for(result.entity, result.projection) is not schema:
        raise RecapProtocolError("Query result schema does not match requested schema")
    payload = result.model_dump()
    payload.pop("entity")
    payload.pop("projection")
    for record in payload["entities"]:
        record["fields"] = _decode_scalars(record["fields"])
    return EntityBatch.model_validate(payload)


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity: str
    projection: Literal["full", "ref"]
    namespace_path: str
    spec: dict[str, Any]

    @classmethod
    def from_query(
        cls, schema: type[BaseModel], spec: QuerySpec, *, namespace_path: str
    ):
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
        )

    @property
    def wire_schema(self) -> type[BaseModel]:
        return schema_for(self.entity, self.projection)

    @model_validator(mode="after")
    def validate_entity(self):
        schema_for(self.entity, self.projection)
        return self


def _encode_scalars(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _encode_scalars(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_encode_scalars(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return {_SCALAR_TAG: "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return _encode_scalars(value.value)
    return value


def _decode_scalars(value: Any) -> Any:
    if isinstance(value, Mapping):
        if (
            len(value) == 2
            and value.get(_SCALAR_TAG) == "datetime"
            and isinstance(value.get("value"), str)
        ):
            return datetime.fromisoformat(value["value"])
        return {key: _decode_scalars(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_decode_scalars(item) for item in value]
    return value
