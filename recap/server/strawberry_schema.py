"""Root GraphQL Query type and schema builder."""

from typing import Annotated

import strawberry
from fastapi import Header, Request
from strawberry.fastapi import GraphQLRouter
from strawberry.scalars import JSON
from strawberry.schema.config import StrawberryConfig

from recap.adapter.local import LocalBackend
from recap.server.context import StrawberryGraphQLContext, graphql_context
from recap.server.resolvers import (
    resolve_execute_count,
    resolve_execute_query,
    resolve_namespaces,
    resolve_namespaces_count,
    resolve_permissions,
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
    NamespaceType,
    PermissionsType,
    ProcessRunType,
    ProcessTemplateType,
    ResourceTemplateType,
    ResourceType,
)


@strawberry.type
class Query:
    execute_query: JSON = strawberry.field(resolver=resolve_execute_query)
    execute_count: int = strawberry.field(resolver=resolve_execute_count)
    permissions: PermissionsType = strawberry.field(resolver=resolve_permissions)

    # List fields
    resources: list[ResourceType] = strawberry.field(resolver=resolve_resources)
    resource_templates: list[ResourceTemplateType] = strawberry.field(
        resolver=resolve_resource_templates
    )
    process_runs: list[ProcessRunType] = strawberry.field(resolver=resolve_process_runs)
    process_templates: list[ProcessTemplateType] = strawberry.field(
        resolver=resolve_process_templates
    )
    namespaces: list[NamespaceType] = strawberry.field(resolver=resolve_namespaces)

    # Count fields
    resources_count: int = strawberry.field(resolver=resolve_resources_count)
    process_runs_count: int = strawberry.field(resolver=resolve_process_runs_count)
    namespaces_count: int = strawberry.field(resolver=resolve_namespaces_count)
    resource_templates_count: int = strawberry.field(
        resolver=resolve_resource_templates_count
    )
    process_templates_count: int = strawberry.field(
        resolver=resolve_process_templates_count
    )


def build_schema(backend: LocalBackend) -> strawberry.Schema:
    """Build a Strawberry schema for introspection or testing only.

    For serving with FastAPI, use ``build_router()`` which properly injects
    context via ``context_getter``. This function returns a bare schema without
    context injection — resolvers accessing ``info.context["backend"]`` will
    fail at runtime unless context is supplied externally.
    """
    return strawberry.Schema(
        query=Query, config=StrawberryConfig(auto_camel_case=False)
    )


def build_router(backend: LocalBackend) -> GraphQLRouter:
    """Build a GraphQLRouter (for mounting in FastAPI) with backend in context."""

    async def get_context(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> StrawberryGraphQLContext:
        return await graphql_context(request, backend, authorization)

    schema = strawberry.Schema(
        query=Query, config=StrawberryConfig(auto_camel_case=False)
    )
    return GraphQLRouter(schema, context_getter=get_context)
