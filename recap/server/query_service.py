from __future__ import annotations

from types import UnionType
from typing import Any, Union, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel, TypeAdapter, ValidationError

from recap.adapter import AuthorizedReadBackend
from recap.adapter.schema_registry import (
    SCHEMA_ENTITY_KEYS,
    SCHEMA_PROJECTIONS,
    SCHEMA_REGISTRY,
    SCHEMAS_BY_NAME,
)
from recap.adapter.transport import QueryRequest, QueryResult, serialize_model
from recap.authorization.query import AuthorizedQuery
from recap.client.permissions import ActorPermissions
from recap.dsl.query import QuerySpec
from recap.exceptions import RecapProtocolError, RecapValidationError


class QueryService:
    def __init__(self, backend: AuthorizedReadBackend) -> None:
        self.backend = backend

    @classmethod
    def legacy_request(cls, schema_name: str, namespace_path: str, spec: Any) -> QueryRequest:
        try:
            schema = SCHEMAS_BY_NAME[schema_name]
        except KeyError as exc:
            raise RecapValidationError("Unknown query schema") from exc
        return QueryRequest(
            entity=SCHEMA_ENTITY_KEYS[schema],
            projection=SCHEMA_PROJECTIONS[schema],
            namespace_path=namespace_path,
            spec=spec,
            schema_name=schema_name,
        )

    def query(self, request: QueryRequest, *, actor, policy) -> QueryResult:
        self._schema(request)
        items = self.query_models(request, actor=actor, policy=policy)
        return QueryResult(
            entity=request.entity,
            projection=request.projection,
            items=[self._serialize_item(item, request.entity, request.projection) for item in items],
        )

    @staticmethod
    def _serialize_item(item, entity, projection):
        payload = serialize_model(item)
        if projection != "ref":
            return payload
        fields = {
            "namespace": {"id", "path"},
            "resource_template": {"id", "name", "slug", "version", "labels", "types", "namespace_id", "status", "revision", "create_date", "modified_date"},
            "resource": {"id", "name", "copied_from_id", "template", "namespace_id", "status", "revision", "create_date", "modified_date"},
            "process_template": {"id", "name", "version", "labels", "namespace_id", "status", "revision", "create_date", "modified_date"},
            "process_run": {"id", "name", "description", "template", "namespace_id", "status", "revision", "create_date", "modified_date"},
        }[entity]
        return {key: value for key, value in payload.items() if key in fields}

    def query_models(self, request: QueryRequest, *, actor, policy):
        schema = self._schema(request)
        spec = self._spec(request, schema)
        if policy is None:
            items = self.backend.query(schema, spec, namespace_path=request.namespace_path)
        else:
            authorization = AuthorizedQuery.from_policy(
                policy, actor, namespace_path=request.namespace_path
            )
            if not spec.include_mutable:
                authorization = authorization.for_read()
            items = self.backend.query_authorized(schema, spec, authorization=authorization)
        return items

    def legacy_query(self, schema_name: str, namespace_path: str, spec: Any, *, backend=None) -> QueryResult:
        request = self.legacy_request(schema_name, namespace_path, spec)
        service = self if backend is None else QueryService(backend)
        return service.query(request, actor=None, policy=None).model_copy(
            update={"schema_name": schema_name}
        )

    def count(self, request: QueryRequest, *, actor, policy) -> int:
        schema = self._schema(request)
        spec = self._spec(request, schema)
        if policy is None:
            value = self.backend.count(schema, spec, namespace_path=request.namespace_path)
        else:
            authorization = AuthorizedQuery.from_policy(
                policy, actor, namespace_path=request.namespace_path
            )
            if not spec.include_mutable:
                authorization = authorization.for_read()
            value = self.backend.count_authorized(schema, spec, authorization=authorization)
        return self.parse_count(value)

    def legacy_count(self, schema_name: str, namespace_path: str, spec: Any, *, backend=None) -> int:
        request = self.legacy_request(schema_name, namespace_path, spec)
        service = self if backend is None else QueryService(backend)
        return service.count(request, actor=None, policy=None)

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
    def parse_result(body: Any) -> QueryResult:
        if not body:
            raise RecapProtocolError("Empty query response body")
        try:
            return QueryResult.model_validate(body)
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
                if value is not None and annotation is not None and QueryService._contains_uuid(annotation):
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
        return annotation is UUID or any(QueryService._contains_uuid(arg) for arg in get_args(annotation))

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
                (result for arg in get_args(annotation) if arg is not type(None)
                 for result in [QueryService._terminal_annotation(arg, path)] if result is not None),
                None,
            )
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            field = annotation.model_fields.get(path[0])
            return QueryService._terminal_annotation(field.annotation, path[1:]) if field else None
        return None
