"""GraphQL read-only adapter for RecapClient."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast
from uuid import UUID

import httpx2 as httpx
from pydantic import SecretStr

from recap.adapter.transport import QueryRequest, QueryResult, hydrate_result
from recap.client.permissions import ActorPermissions
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
_PERMISSIONS = (
    "query Permissions($namespace_path: String!) "
    "{ permissions(namespace_path: $namespace_path) "
    "{ identities { provider subject } snapshot_generation effective_scopes "
    "matched_namespace_paths groups roles } }"
)


class _RedactedAuthHeaders:
    def __init__(self, api_key: str | SecretStr | None) -> None:
        self._api_key = (
            api_key
            if isinstance(api_key, SecretStr)
            else SecretStr(api_key)
            if api_key is not None
            else None
        )

    def as_dict(self) -> dict[str, str]:
        if self._api_key is None:
            return {}
        return {"Authorization": f"Apikey {self._api_key.get_secret_value()}"}

    def redact(self, value: str) -> str:
        if self._api_key is None:
            return value
        return value.replace(self._api_key.get_secret_value(), "**********")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(api_key=SecretStr('**********'))"


def _check_graphql_errors(
    body: Mapping[str, Any], *, headers: _RedactedAuthHeaders
) -> None:
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
    message = "; ".join(str(message) for message in messages)
    raise RuntimeError(f"GraphQL request failed: {headers.redact(message)}")


class GraphQLAdapter:
    """ReadBackend implementation over HTTP GraphQL.

    Sends QuerySpec through the transport codec and hydrates returned schemas.

    Phase 1 constraint: read-only. Write methods raise NotImplementedError.
    Use LocalBackend (via RecapClient.from_url()) for writes.
    """

    def __init__(
        self,
        graphql_url: str,
        api_key: str | SecretStr | None = None,
        *,
        _header_provider: _RedactedAuthHeaders | None = None,
    ):
        self._url = graphql_url
        self._headers = _header_provider or _RedactedAuthHeaders(api_key)
        self._client = httpx.Client(timeout=30.0)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(graphql_url={self._url!r}, "
            f"headers={self._headers!r})"
        )

    def close(self) -> None:
        """Close the underlying HTTP client and release connections."""
        self._client.close()

    def __enter__(self) -> GraphQLAdapter:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _post(self, payload: Mapping[str, Any]):
        try:
            response = self._client.post(
                self._url,
                json=payload,
                headers=self._headers.as_dict(),
            )
            response.raise_for_status()
        except Exception as exc:
            message = self._headers.redact(str(exc))
            raise RuntimeError(f"GraphQL request failed: {message}") from None
        return response

    def query(
        self, schema: type[SchemaT], spec: QuerySpec, *, namespace_path: str
    ) -> list[SchemaT]:
        request = QueryRequest.from_query(schema, spec, namespace_path=namespace_path)
        response = self._post(
            {
                "query": _EXECUTE_QUERY,
                "variables": request.model_dump(mode="json"),
            }
        )
        body = response.json()
        _check_graphql_errors(body, headers=self._headers)
        result = QueryResult.model_validate(body["data"]["execute_query"])
        return cast(list[SchemaT], hydrate_result(schema, result))

    def count(
        self, schema: type[SchemaT], spec: QuerySpec, *, namespace_path: str
    ) -> int:
        request = QueryRequest.from_query(schema, spec, namespace_path=namespace_path)
        response = self._post(
            {
                "query": _EXECUTE_COUNT,
                "variables": request.model_dump(mode="json"),
            }
        )
        body = response.json()
        _check_graphql_errors(body, headers=self._headers)
        return body["data"]["execute_count"]

    def permissions(self, namespace_path: str) -> ActorPermissions:
        response = self._post(
            {
                "query": _PERMISSIONS,
                "variables": {"namespace_path": namespace_path},
            }
        )
        body = response.json()
        _check_graphql_errors(body, headers=self._headers)
        return ActorPermissions.model_validate(body["data"]["permissions"])

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
