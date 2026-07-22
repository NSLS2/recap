"""GraphQL resolver functions. Call LocalBackend directly — no RecapClient, no QueryDSL."""
from __future__ import annotations

from uuid import UUID

import strawberry

from recap.adapter.local import LocalBackend
from recap.dsl.query import QuerySpec
from recap.schemas.process import CampaignSchema, ProcessRunSchema, ProcessTemplateSchema
from recap.schemas.resource import ResourceSchema, ResourceTemplateSchema
from recap.server.strawberry_types import (
    CampaignType,
    ProcessRunType,
    ProcessTemplateType,
    ResourceTemplateType,
    ResourceType,
)

_DEFAULT_LIMIT = 1000
_MAX_LIMIT = 10_000


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


def _campaign_schema_to_type(c: CampaignSchema) -> CampaignType:
    return CampaignType(
        id=strawberry.ID(str(c.id)),
        name=c.name,
        proposal=getattr(c, "proposal", None),
        create_date=c.create_date,
        modified_date=c.modified_date,
    )


def _process_template_schema_to_type(pt: ProcessTemplateSchema) -> ProcessTemplateType:
    return ProcessTemplateType(
        id=strawberry.ID(str(pt.id)),
        name=pt.name,
        version=getattr(pt, "version", "1.0"),
        create_date=pt.create_date,
        modified_date=pt.modified_date,
    )


def _resource_template_schema_to_type(r: ResourceTemplateSchema) -> ResourceTemplateType:
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
    campaign_id: strawberry.ID | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[ResourceType]:
    backend: LocalBackend = info.context["backend"]
    effective_limit = _check_limit(limit)
    spec = QuerySpec(
        campaign_id=UUID(str(campaign_id)) if campaign_id else None,
        limit=effective_limit,
        offset=offset,
    )
    results = backend.query(ResourceSchema, spec)
    return [_resource_schema_to_type(r) for r in results]


def resolve_resources_count(
    info: strawberry.types.Info,
    campaign_id: strawberry.ID | None = None,
) -> int:
    backend: LocalBackend = info.context["backend"]
    spec = QuerySpec(campaign_id=UUID(str(campaign_id)) if campaign_id else None)
    return backend.count(ResourceSchema, spec)


def resolve_resource_templates(
    info: strawberry.types.Info,
    limit: int | None = None,
    offset: int | None = None,
) -> list[ResourceTemplateType]:
    backend: LocalBackend = info.context["backend"]
    effective_limit = _check_limit(limit)
    spec = QuerySpec(limit=effective_limit, offset=offset)
    results = backend.query(ResourceTemplateSchema, spec)
    return [_resource_template_schema_to_type(r) for r in results]


def resolve_process_runs(
    info: strawberry.types.Info,
    campaign_id: strawberry.ID | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[ProcessRunType]:
    backend: LocalBackend = info.context["backend"]
    effective_limit = _check_limit(limit)
    spec = QuerySpec(
        campaign_id=UUID(str(campaign_id)) if campaign_id else None,
        limit=effective_limit,
        offset=offset,
    )
    results = backend.query(ProcessRunSchema, spec)
    return [_process_run_schema_to_type(pr) for pr in results]


def resolve_process_runs_count(
    info: strawberry.types.Info,
    campaign_id: strawberry.ID | None = None,
) -> int:
    backend: LocalBackend = info.context["backend"]
    spec = QuerySpec(campaign_id=UUID(str(campaign_id)) if campaign_id else None)
    return backend.count(ProcessRunSchema, spec)


def resolve_process_templates(
    info: strawberry.types.Info,
    limit: int | None = None,
    offset: int | None = None,
) -> list[ProcessTemplateType]:
    backend: LocalBackend = info.context["backend"]
    effective_limit = _check_limit(limit)
    spec = QuerySpec(limit=effective_limit, offset=offset)
    results = backend.query(ProcessTemplateSchema, spec)
    return [_process_template_schema_to_type(pt) for pt in results]


def resolve_campaigns(
    info: strawberry.types.Info,
    limit: int | None = None,
    offset: int | None = None,
) -> list[CampaignType]:
    backend: LocalBackend = info.context["backend"]
    effective_limit = _check_limit(limit)
    spec = QuerySpec(limit=effective_limit, offset=offset)
    results = backend.query(CampaignSchema, spec)
    return [_campaign_schema_to_type(c) for c in results]


def resolve_campaigns_count(info: strawberry.types.Info) -> int:
    backend: LocalBackend = info.context["backend"]
    spec = QuerySpec()
    return backend.count(CampaignSchema, spec)
