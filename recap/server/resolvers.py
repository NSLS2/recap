"""GraphQL resolver functions. Call LocalBackend directly — no RecapClient, no QueryDSL."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

import strawberry
from pydantic import BaseModel, ValidationError
from strawberry.scalars import JSON

from recap.adapter.local import LocalBackend
from recap.adapter.transport import QueryResult, serialize_model
from recap.dsl.query import QuerySpec
from recap.schemas.namespace import NamespaceSchema
from recap.schemas.process import (
    ProcessRunRef,
    ProcessRunSchema,
    ProcessTemplateRef,
    ProcessTemplateSchema,
)
from recap.schemas.resource import (
    ResourceRef,
    ResourceSchema,
    ResourceTemplateRef,
    ResourceTemplateSchema,
)
from recap.server.strawberry_types import (
    NamespaceType,
    ProcessRunType,
    ProcessTemplateType,
    ResourceTemplateType,
    ResourceType,
)

_DEFAULT_LIMIT = 1000
_MAX_LIMIT = 10_000

_SCHEMA_REGISTRY: Mapping[str, type[BaseModel]] = MappingProxyType(
    {
        "ResourceSchema": ResourceSchema,
        "ResourceRef": ResourceRef,
        "ResourceTemplateSchema": ResourceTemplateSchema,
        "ResourceTemplateRef": ResourceTemplateRef,
        "ProcessRunSchema": ProcessRunSchema,
        "ProcessRunRef": ProcessRunRef,
        "ProcessTemplateSchema": ProcessTemplateSchema,
        "ProcessTemplateRef": ProcessTemplateRef,
        "NamespaceSchema": NamespaceSchema,
    }
)


def _resolve_schema(schema_name: str) -> type[BaseModel]:
    try:
        return _SCHEMA_REGISTRY[schema_name]
    except KeyError as exc:
        raise strawberry.exceptions.StrawberryGraphQLError(
            "Unknown query schema"
        ) from exc


def _validate_query_spec(spec: JSON) -> QuerySpec:
    try:
        return QuerySpec.model_validate(spec)
    except ValidationError as exc:
        raise strawberry.exceptions.StrawberryGraphQLError(
            "Invalid query specification"
        ) from exc


def resolve_execute_query(
    info: strawberry.types.Info, schema_name: str, namespace_path: str, spec: JSON
) -> JSON:
    backend: LocalBackend = info.context["backend"]
    schema = _resolve_schema(schema_name)
    query_spec = _validate_query_spec(spec)
    items = [
        serialize_model(item)
        for item in backend.query(schema, query_spec, namespace_path=namespace_path)
    ]
    return QueryResult(schema_name=schema_name, items=items).model_dump(mode="json")


def resolve_execute_count(
    info: strawberry.types.Info, schema_name: str, namespace_path: str, spec: JSON
) -> int:
    backend: LocalBackend = info.context["backend"]
    schema = _resolve_schema(schema_name)
    return backend.count(
        schema, _validate_query_spec(spec), namespace_path=namespace_path
    )


def _resource_schema_to_type(r: ResourceSchema) -> ResourceType:
    return ResourceType(
        id=strawberry.ID(str(r.id)),
        name=r.name,
        create_date=r.create_date,
        modified_date=r.modified_date,
    )


def _process_run_schema_to_type(pr: ProcessRunSchema) -> ProcessRunType:
    return ProcessRunType(
        id=strawberry.ID(str(pr.id)),
        name=pr.name,
        description=getattr(pr, "description", None),
        create_date=pr.create_date,
        modified_date=pr.modified_date,
    )


def _namespace_schema_to_type(namespace: NamespaceSchema) -> NamespaceType:
    return NamespaceType(
        id=strawberry.ID(str(namespace.id)),
        path=namespace.path,
        parent_id=(
            strawberry.ID(str(namespace.parent_id)) if namespace.parent_id else None
        ),
        status=namespace.status.value,
        revision=namespace.revision,
        metadata=namespace.metadata,
        create_date=namespace.create_date,
        modified_date=namespace.modified_date,
    )


def _process_template_schema_to_type(pt: ProcessTemplateSchema) -> ProcessTemplateType:
    return ProcessTemplateType(
        id=strawberry.ID(str(pt.id)),
        name=pt.name,
        version=getattr(pt, "version", "1.0"),
        create_date=pt.create_date,
        modified_date=pt.modified_date,
    )


def _resource_template_schema_to_type(
    r: ResourceTemplateSchema,
) -> ResourceTemplateType:
    return ResourceTemplateType(
        id=strawberry.ID(str(r.id)),
        name=r.name,
        version=getattr(r, "version", "1.0"),
        create_date=r.create_date,
        modified_date=r.modified_date,
    )


def _check_limit(limit: int | None) -> int:
    effective = limit if limit is not None else _DEFAULT_LIMIT
    if effective > _MAX_LIMIT:
        raise strawberry.exceptions.StrawberryGraphQLError(
            f"Requested limit {effective} exceeds maximum allowed {_MAX_LIMIT}"
        )
    return effective


def resolve_resources(
    info: strawberry.types.Info,
    namespace_path: str,
    limit: int | None = None,
    offset: int | None = None,
) -> list[ResourceType]:
    backend: LocalBackend = info.context["backend"]
    effective_limit = _check_limit(limit)
    spec = QuerySpec(limit=effective_limit, offset=offset)
    results = backend.query(ResourceSchema, spec, namespace_path=namespace_path)
    return [_resource_schema_to_type(r) for r in results]


def resolve_resources_count(
    info: strawberry.types.Info,
    namespace_path: str,
) -> int:
    backend: LocalBackend = info.context["backend"]
    return backend.count(ResourceSchema, QuerySpec(), namespace_path=namespace_path)


def resolve_resource_templates(
    info: strawberry.types.Info,
    namespace_path: str,
    limit: int | None = None,
    offset: int | None = None,
) -> list[ResourceTemplateType]:
    backend: LocalBackend = info.context["backend"]
    effective_limit = _check_limit(limit)
    spec = QuerySpec(limit=effective_limit, offset=offset)
    results = backend.query(ResourceTemplateSchema, spec, namespace_path=namespace_path)
    return [_resource_template_schema_to_type(r) for r in results]


def resolve_process_runs(
    info: strawberry.types.Info,
    namespace_path: str,
    limit: int | None = None,
    offset: int | None = None,
) -> list[ProcessRunType]:
    backend: LocalBackend = info.context["backend"]
    effective_limit = _check_limit(limit)
    spec = QuerySpec(limit=effective_limit, offset=offset)
    results = backend.query(ProcessRunSchema, spec, namespace_path=namespace_path)
    return [_process_run_schema_to_type(pr) for pr in results]


def resolve_process_runs_count(
    info: strawberry.types.Info,
    namespace_path: str,
) -> int:
    backend: LocalBackend = info.context["backend"]
    return backend.count(ProcessRunSchema, QuerySpec(), namespace_path=namespace_path)


def resolve_process_templates(
    info: strawberry.types.Info,
    namespace_path: str,
    limit: int | None = None,
    offset: int | None = None,
) -> list[ProcessTemplateType]:
    backend: LocalBackend = info.context["backend"]
    effective_limit = _check_limit(limit)
    spec = QuerySpec(limit=effective_limit, offset=offset)
    results = backend.query(ProcessTemplateSchema, spec, namespace_path=namespace_path)
    return [_process_template_schema_to_type(pt) for pt in results]


def resolve_namespaces(
    info: strawberry.types.Info,
    namespace_path: str,
    limit: int | None = None,
    offset: int | None = None,
) -> list[NamespaceType]:
    backend: LocalBackend = info.context["backend"]
    effective_limit = _check_limit(limit)
    spec = QuerySpec(limit=effective_limit, offset=offset)
    results = backend.query(NamespaceSchema, spec, namespace_path=namespace_path)
    return [_namespace_schema_to_type(namespace) for namespace in results]


def resolve_namespaces_count(info: strawberry.types.Info, namespace_path: str) -> int:
    backend: LocalBackend = info.context["backend"]
    spec = QuerySpec()
    return backend.count(NamespaceSchema, spec, namespace_path=namespace_path)


def resolve_resource_templates_count(
    info: strawberry.types.Info, namespace_path: str
) -> int:
    backend = info.context["backend"]
    spec = QuerySpec()
    return backend.count(ResourceTemplateSchema, spec, namespace_path=namespace_path)


def resolve_process_templates_count(
    info: strawberry.types.Info, namespace_path: str
) -> int:
    backend = info.context["backend"]
    spec = QuerySpec()
    return backend.count(ProcessTemplateSchema, spec, namespace_path=namespace_path)
