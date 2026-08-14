"""GraphQL read-only adapter for RecapClient."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast
from uuid import UUID

from pydantic import SecretStr, ValidationError

from recap.adapter.http_transport import HTTPResult, HTTPTransport
from recap.adapter.transport import QueryRequest, QueryResult, hydrate_result
from recap.client.permissions import ActorPermissions
from recap.dsl.query import QuerySpec, SchemaT
from recap.exceptions import RecapProtocolError, error_from_code
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


def _check_graphql_errors(
    body: Mapping[str, Any], *, url: str, fallback_request_id: str | None,
    redact: Any,
) -> None:
    if "errors" not in body:
        return
    errors = body["errors"]
    if (
        not isinstance(errors, Sequence)
        or isinstance(errors, str | bytes)
        or not all(isinstance(error, Mapping) for error in errors)
    ):
        raise RecapProtocolError(
            "Malformed GraphQL error response", url=url, request_id=fallback_request_id
        )
    if not errors:
        return

    messages: list[str] = []
    first_code: str | None = None
    request_id: str | None = None
    for error in errors:
        message = error.get("message")
        extensions = error.get("extensions")
        code = extensions.get("code") if isinstance(extensions, Mapping) else None
        error_request_id = (
            extensions.get("request_id") if isinstance(extensions, Mapping) else None
        )
        if (
            not isinstance(message, str)
            or not isinstance(extensions, Mapping)
            or not isinstance(code, str)
            or code not in _ERROR_CODES
            or not isinstance(error_request_id, str)
        ):
            raise RecapProtocolError(
                "Malformed GraphQL error response",
                url=url,
                request_id=fallback_request_id,
            )
        first_code = first_code or code
        request_id = request_id or error_request_id
        messages.append(redact(message))

    raise error_from_code(
        first_code,
        "; ".join(messages),
        url=url,
        request_id=request_id or fallback_request_id,
    )


_ERROR_CODES = {
    "authentication_required",
    "permission_denied",
    "not_found",
    "validation_error",
    "conflict",
    "service_unavailable",
    "internal_error",
    "request_error",
}


class GraphQLAdapter:
    """ReadBackend implementation over HTTP GraphQL."""

    def __init__(
        self,
        graphql_url: str,
        api_key: str | SecretStr | None = None,
        *,
        timeout: float = 30.0,
        _transport: HTTPTransport | None = None,
    ) -> None:
        self._url = graphql_url
        self._transport = (
            _transport if _transport is not None else HTTPTransport(api_key, timeout=timeout)
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(graphql_url={self._url!r}, transport={self._transport!r})"

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> GraphQLAdapter:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _post(self, payload: Mapping[str, Any]) -> HTTPResult:
        return self._transport.request("POST", self._url, json=payload)

    def _body(self, response: HTTPResult) -> Mapping[str, Any]:
        if not isinstance(response.body, Mapping):
            raise RecapProtocolError(
                "Malformed GraphQL response", url=self._url, request_id=response.request_id
            )
        _check_graphql_errors(
            response.body, url=self._url, fallback_request_id=response.request_id,
            redact=self._transport.redact,
        )
        return response.body

    def query(
        self, schema: type[SchemaT], spec: QuerySpec, *, namespace_path: str
    ) -> list[SchemaT]:
        request = QueryRequest.from_query(schema, spec, namespace_path=namespace_path)
        response = self._post({
            "query": _EXECUTE_QUERY,
            "variables": request.model_dump(mode="json"),
        })
        try:
            body = self._body(response)
            result = QueryResult.model_validate(body["data"]["execute_query"])
            return cast(list[SchemaT], hydrate_result(schema, result))
        except (KeyError, TypeError, ValueError, ValidationError):
            raise RecapProtocolError(
                "Malformed GraphQL response", url=self._url, request_id=response.request_id
            ) from None

    def count(
        self, schema: type[SchemaT], spec: QuerySpec, *, namespace_path: str
    ) -> int:
        request = QueryRequest.from_query(schema, spec, namespace_path=namespace_path)
        response = self._post({
            "query": _EXECUTE_COUNT,
            "variables": request.model_dump(mode="json"),
        })
        try:
            body = self._body(response)
            count = body["data"]["execute_count"]
            if not isinstance(count, int) or isinstance(count, bool):
                raise TypeError
            return count
        except (KeyError, TypeError, ValueError, ValidationError):
            raise RecapProtocolError(
                "Malformed GraphQL response", url=self._url, request_id=response.request_id
            ) from None

    def permissions(self, namespace_path: str) -> ActorPermissions:
        response = self._post({
            "query": _PERMISSIONS,
            "variables": {"namespace_path": namespace_path},
        })
        try:
            body = self._body(response)
            return ActorPermissions.model_validate(body["data"]["permissions"])
        except (KeyError, TypeError, ValueError, ValidationError):
            raise RecapProtocolError(
                "Malformed GraphQL response", url=self._url, request_id=response.request_id
            ) from None

    def get_resource(self, namespace_id: UUID, name: str, template_name: str,
                     template_version: str | None = "1.0", expand: bool = False) -> ResourceSchema:
        raise NotImplementedError("get_resource via GraphQL not yet implemented — use query()")

    def get_resource_template(self, namespace_id: UUID, name: str | None, version: str | None = None,
                              id: UUID | str | None = None, parent=None, expand=False):
        raise NotImplementedError("get_resource_template via GraphQL not yet implemented — use query()")

    def get_process_template(self, namespace_id: UUID, name: str | None, version: str | None,
                             expand=False, id: UUID | str | None = None):
        raise NotImplementedError("get_process_template via GraphQL not yet implemented — use query()")

    def find_resources_by_identity(self, namespace_id: UUID, name: str, parent_id: UUID | None,
                                   resource_template_id: UUID) -> list:
        raise NotImplementedError("find_resources_by_identity via GraphQL not yet implemented")

    def get_steps(self, process_run: ProcessRunSchema) -> list[StepSchema]:
        raise NotImplementedError("get_steps via GraphQL not yet implemented")

    def get_params(self, step_schema: StepSchema):
        raise NotImplementedError("get_params via GraphQL not yet implemented")
