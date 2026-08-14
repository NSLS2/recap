"""Root GraphQL Query type and schema builder."""

from typing import Annotated

import strawberry
from fastapi import Depends, Header, Request
from strawberry.fastapi import GraphQLRouter
from strawberry.scalars import JSON
from strawberry.schema.config import StrawberryConfig

from recap.adapter import AuthorizedReadBackend, ReadBackend
from recap.server.context import StrawberryGraphQLContext, graphql_context
from recap.server.dependencies import get_local_backend
from recap.server.errors import ErrorCode, request_id_from
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


def build_schema(backend: ReadBackend) -> strawberry.Schema:
    """Build a Strawberry schema for introspection or testing only.

    For serving with FastAPI, use ``build_router()`` which properly injects
    context via ``context_getter``. This function returns a bare schema without
    context injection — resolvers accessing ``info.context["backend"]`` will
    fail at runtime unless context is supplied externally.
    """
    return strawberry.Schema(
        query=Query, config=StrawberryConfig(auto_camel_case=False)
    )


class SafeGraphQLRouter(GraphQLRouter):
    async def process_result(self, request: Request, result):
        response = await super().process_result(request, result)
        request_id = request_id_from(request)
        errors = response.get("errors")
        if not errors:
            return response

        for error in errors:
            extensions = error.get("extensions") or {}
            code = extensions.get("code")
            if code == ErrorCode.VALIDATION_ERROR.value:
                message = error["message"]
            elif code == ErrorCode.INTERNAL_ERROR.value:
                code = ErrorCode.INTERNAL_ERROR.value
                message = "Internal server error"
            else:
                code = (
                    ErrorCode.VALIDATION_ERROR.value
                    if "path" not in error
                    else ErrorCode.INTERNAL_ERROR.value
                )
                message = (
                    "GraphQL request validation failed"
                    if code == ErrorCode.VALIDATION_ERROR.value
                    else "Internal server error"
                )
            error["message"] = message
            error["extensions"] = {"code": code, "request_id": request_id}

        return response


def build_router() -> GraphQLRouter:
    """Build a GraphQLRouter with request-scoped backend injection."""

    async def get_context(
        request: Request,
        backend: Annotated[AuthorizedReadBackend, Depends(get_local_backend)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> StrawberryGraphQLContext:
        return await graphql_context(request, backend, authorization)

    schema = strawberry.Schema(
        query=Query, config=StrawberryConfig(auto_camel_case=False)
    )
    return SafeGraphQLRouter(schema, context_getter=get_context)
