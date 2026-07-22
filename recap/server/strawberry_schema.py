"""Root GraphQL Query type and schema builder."""

import strawberry
from strawberry.fastapi import GraphQLRouter

from recap.adapter.local import LocalBackend
from recap.server.strawberry_types import (
    CampaignType,
    ProcessRunType,
    ProcessTemplateType,
    ResourceTemplateType,
    ResourceType,
)


# Stub resolvers — replaced when recap/server/resolvers.py is created in Task 6.
def resolve_resources(info: strawberry.types.Info) -> list[ResourceType]:
    raise NotImplementedError("resolvers.py not yet implemented")


def resolve_resource_templates(info: strawberry.types.Info) -> list[ResourceTemplateType]:
    raise NotImplementedError("resolvers.py not yet implemented")


def resolve_process_runs(info: strawberry.types.Info) -> list[ProcessRunType]:
    raise NotImplementedError("resolvers.py not yet implemented")


def resolve_process_templates(info: strawberry.types.Info) -> list[ProcessTemplateType]:
    raise NotImplementedError("resolvers.py not yet implemented")


def resolve_campaigns(info: strawberry.types.Info) -> list[CampaignType]:
    raise NotImplementedError("resolvers.py not yet implemented")


def resolve_resources_count(info: strawberry.types.Info) -> int:
    raise NotImplementedError("resolvers.py not yet implemented")


def resolve_process_runs_count(info: strawberry.types.Info) -> int:
    raise NotImplementedError("resolvers.py not yet implemented")


def resolve_campaigns_count(info: strawberry.types.Info) -> int:
    raise NotImplementedError("resolvers.py not yet implemented")


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
