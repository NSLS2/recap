"""GraphQL read-only adapter for RecapClient."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast
from uuid import UUID

import httpx2 as httpx

from recap.adapter.transport import QueryRequest, QueryResult, hydrate_result
from recap.dsl.query import QuerySpec, SchemaT
from recap.schemas.process import ProcessRunSchema
from recap.schemas.resource import ResourceSchema
from recap.schemas.step import StepSchema

_EXECUTE_QUERY = (
    "query ExecuteQuery($schema_name: String!, $namespace_path: String!, $spec: JSON!) "
    "{ execute_query(schema_name: $schema_name, namespace_path: $namespace_path, spec: $spec) }"
)
_EXECUTE_COUNT = (
    "query ExecuteCount($schema_name: String!, $namespace_path: String!, $spec: JSON!) "
    "{ execute_count(schema_name: $schema_name, namespace_path: $namespace_path, spec: $spec) }"
)


def _check_graphql_errors(body: Mapping[str, Any]) -> None:
    errors = body.get("errors")
    if not errors:
        return
    if (
        not isinstance(errors, Sequence)
        or isinstance(errors, str | bytes)
        or not all(isinstance(error, Mapping) for error in errors)
    ):
        raise RuntimeError("GraphQL request failed: malformed error response")
    messages = [error.get("message", "Unknown GraphQL error") for error in errors]
    raise RuntimeError(f"GraphQL request failed: {'; '.join(messages)}")


class GraphQLAdapter:
    """ReadBackend implementation over HTTP GraphQL.

    Sends QuerySpec through the transport codec and hydrates returned schemas.

    Phase 1 constraint: read-only. Write methods raise NotImplementedError.
    Use LocalBackend (via RecapClient.from_url()) for writes.
    """

    def __init__(self, graphql_url: str):
        self._url = graphql_url
        self._client = httpx.Client(timeout=30.0)

    def close(self) -> None:
        """Close the underlying HTTP client and release connections."""
        self._client.close()

    def __enter__(self) -> GraphQLAdapter:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def query(
        self, schema: type[SchemaT], spec: QuerySpec, *, namespace_path: str
    ) -> list[SchemaT]:
        request = QueryRequest.from_query(schema, spec, namespace_path=namespace_path)
        response = self._client.post(
            self._url,
            json={
                "query": _EXECUTE_QUERY,
                "variables": request.model_dump(mode="json"),
            },
        )
        response.raise_for_status()
        body = response.json()
        _check_graphql_errors(body)
        result = QueryResult.model_validate(body["data"]["execute_query"])
        return cast(list[SchemaT], hydrate_result(schema, result))

    def count(
        self, schema: type[SchemaT], spec: QuerySpec, *, namespace_path: str
    ) -> int:
        request = QueryRequest.from_query(schema, spec, namespace_path=namespace_path)
        response = self._client.post(
            self._url,
            json={
                "query": _EXECUTE_COUNT,
                "variables": request.model_dump(mode="json"),
            },
        )
        response.raise_for_status()
        body = response.json()
        _check_graphql_errors(body)
        return body["data"]["execute_count"]

    # ------------------------------------------------------------------ #
    # Read methods delegated to server (minimal implementations for now)
    # Full implementations to be added as needed in follow-up tasks.
    # ------------------------------------------------------------------ #

    def get_resource(
        self,
        namespace_id: UUID,
        name: str,
        template_name: str,
        template_version: str | None = "1.0",
        expand: bool = False,
    ) -> ResourceSchema:
        raise NotImplementedError(
            "get_resource via GraphQL not yet implemented — use query()"
        )

    def get_resource_template(
        self,
        namespace_id: UUID,
        name: str | None,
        version: str | None = None,
        id: UUID | str | None = None,
        parent=None,
        expand=False,
    ):
        raise NotImplementedError(
            "get_resource_template via GraphQL not yet implemented — use query()"
        )

    def get_process_template(
        self,
        namespace_id: UUID,
        name: str | None,
        version: str | None,
        expand=False,
        id: UUID | str | None = None,
    ):
        raise NotImplementedError(
            "get_process_template via GraphQL not yet implemented — use query()"
        )

    def find_resources_by_identity(
        self,
        namespace_id: UUID,
        name: str,
        parent_id: UUID | None,
        resource_template_id: UUID,
    ) -> list:
        raise NotImplementedError(
            "find_resources_by_identity via GraphQL not yet implemented"
        )

    def get_steps(self, process_run: ProcessRunSchema) -> list[StepSchema]:
        raise NotImplementedError("get_steps via GraphQL not yet implemented")

    def get_params(self, step_schema: StepSchema):
        raise NotImplementedError("get_params via GraphQL not yet implemented")
