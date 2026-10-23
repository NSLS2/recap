from __future__ import annotations

from types import UnionType
from typing import Any, Union, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel, TypeAdapter, ValidationError

from recap.adapter import AuthorizedBatchReadBackend
from recap.adapter.schema_registry import (
    SCHEMA_REGISTRY,
)
from recap.adapter.transport import (
    FlatQueryResult,
    QueryRequest,
    encode_query_batch,
)
from recap.authorization.query import AuthorizedQuery
from recap.client.permissions import ActorPermissions
from recap.dsl.query import QuerySpec
from recap.exceptions import RecapProtocolError, RecapValidationError


class QueryService:
    def __init__(self, backend: AuthorizedBatchReadBackend) -> None:
        self.backend = backend

    def query(self, request: QueryRequest, *, actor, policy) -> FlatQueryResult:
        schema = self._schema(request)
        spec = self._spec(request, schema)
        if policy is None:
            batch = self.backend.query_batch(
                schema, spec, namespace_path=request.namespace_path
            )
        else:
            authorization = AuthorizedQuery.from_policy(
                policy, actor, namespace_path=request.namespace_path
            )
            if not spec.include_mutable:
                authorization = authorization.for_read()
            batch = self.backend.query_batch_authorized(
                schema, spec, authorization=authorization
            )
        return encode_query_batch(
            batch, entity=request.entity, projection=request.projection
        )

    def count(self, request: QueryRequest, *, actor, policy) -> int:
        schema = self._schema(request)
        spec = self._spec(request, schema)
        if policy is None:
            value = self.backend.count(
                schema, spec, namespace_path=request.namespace_path
            )
        else:
            authorization = AuthorizedQuery.from_policy(
                policy, actor, namespace_path=request.namespace_path
            )
            if not spec.include_mutable:
                authorization = authorization.for_read()
            value = self.backend.count_authorized(
                schema, spec, authorization=authorization
            )
        return self.parse_count(value)

    def permissions(self, namespace_path: str, *, actor, policy) -> ActorPermissions:
        permissions = policy.permissions_for(actor, namespace_path)
        return ActorPermissions(
            identities=permissions.identities,
            snapshot_generation=permissions.snapshot_generation,
            effective_scopes=permissions.effective_scopes,
            matched_namespace_paths=permissions.matched_namespace_paths,
            groups=tuple(sorted({grant.group for grant in permissions.grants})),
            roles=tuple(sorted({grant.role for grant in permissions.grants})),
        )

    @staticmethod
    def parse_result(body: Any) -> FlatQueryResult:
        if not body:
            raise RecapProtocolError("Empty query response body")
        try:
            return FlatQueryResult.model_validate(body)
        except ValidationError as exc:
            raise RecapProtocolError("Malformed query response") from exc

    @staticmethod
    def parse_count(value: Any) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise RecapProtocolError("Malformed query count")
        return value

    @staticmethod
    def _schema(request: QueryRequest) -> type[BaseModel]:
        try:
            registration = SCHEMA_REGISTRY.by_key(request.entity)
            if request.projection not in registration.projections:
                raise ValueError(
                    f"Unknown query entity/projection: {request.entity}/{request.projection}"
                )
            return registration.model
        except ValueError as exc:
            raise RecapValidationError(str(exc)) from exc
        except KeyError as exc:
            raise RecapValidationError(str(exc)) from exc

    def _spec(self, request: QueryRequest, schema: type[BaseModel]) -> QuerySpec:
        try:
            spec = QuerySpec.model_validate(request.spec)
            filters = dict(spec.filters)
            for key, value in filters.items():
                annotation = QueryService._terminal_annotation(schema, key.split("__"))
                if (
                    value is not None
                    and annotation is not None
                    and QueryService._contains_uuid(annotation)
                ):
                    filters[key] = TypeAdapter(annotation).validate_python(value)
            spec = spec.model_copy(update={"filters": filters})
            validate_query = getattr(self.backend, "validate_query", None)
            if validate_query is not None:
                validate_query(schema, spec)
            return spec
        except (ValidationError, ValueError, TypeError) as exc:
            raise RecapValidationError("Invalid query specification") from exc

    @staticmethod
    def _contains_uuid(annotation: Any) -> bool:
        return annotation is UUID or any(
            QueryService._contains_uuid(arg) for arg in get_args(annotation)
        )

    @staticmethod
    def _terminal_annotation(annotation: Any, path: list[str]) -> Any | None:
        if not path:
            return annotation
        origin = get_origin(annotation)
        if origin in (list, set, tuple):
            args = get_args(annotation)
            return QueryService._terminal_annotation(args[0], path) if args else None
        if origin in (UnionType, Union):
            return next(
                (
                    result
                    for arg in get_args(annotation)
                    if arg is not type(None)
                    for result in [QueryService._terminal_annotation(arg, path)]
                    if result is not None
                ),
                None,
            )
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            field = annotation.model_fields.get(path[0])
            return (
                QueryService._terminal_annotation(field.annotation, path[1:])
                if field
                else None
            )
        return None
