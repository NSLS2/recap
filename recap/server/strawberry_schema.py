"""Root GraphQL Query type and schema builder."""

import strawberry
from strawberry.fastapi import GraphQLRouter

from recap.adapter.local import LocalBackend
from recap.server.resolvers import (
    resolve_campaigns,
    resolve_campaigns_count,
    resolve_process_runs,
    resolve_process_runs_count,
    resolve_process_templates,
    resolve_process_templates_count,
    resolve_resource_templates,
    resolve_resource_templates_count,
    resolve_resources,
    resolve_resources_count,
)
from recap.server.strawberry_types import (
    CampaignType,
    ProcessRunType,
    ProcessTemplateType,
    ResourceTemplateType,
    ResourceType,
)


@strawberry.type
class Query:
    # List fields
    resources: list[ResourceType] = strawberry.field(resolver=resolve_resources)
    resource_templates: list[ResourceTemplateType] = strawberry.field(resolver=resolve_resource_templates)
    process_runs: list[ProcessRunType] = strawberry.field(resolver=resolve_process_runs)
    process_templates: list[ProcessTemplateType] = strawberry.field(resolver=resolve_process_templates)
    campaigns: list[CampaignType] = strawberry.field(resolver=resolve_campaigns)

    # Count fields
    resources_count: int = strawberry.field(resolver=resolve_resources_count)
    process_runs_count: int = strawberry.field(resolver=resolve_process_runs_count)
    campaigns_count: int = strawberry.field(resolver=resolve_campaigns_count)
    resource_templates_count: int = strawberry.field(resolver=resolve_resource_templates_count)
    process_templates_count: int = strawberry.field(resolver=resolve_process_templates_count)


def build_schema(backend: LocalBackend) -> strawberry.Schema:
    """Build a Strawberry schema for introspection or testing only.

    For serving with FastAPI, use ``build_router()`` which properly injects
    context via ``context_getter``. This function returns a bare schema without
    context injection — resolvers accessing ``info.context["backend"]`` will
    fail at runtime unless context is supplied externally.
    """
    return strawberry.Schema(query=Query)


def build_router(backend: LocalBackend) -> GraphQLRouter:
    """Build a GraphQLRouter (for mounting in FastAPI) with backend in context."""

    async def get_context() -> dict:
        return {"backend": backend}

    schema = strawberry.Schema(query=Query)
    return GraphQLRouter(schema, context_getter=get_context)
