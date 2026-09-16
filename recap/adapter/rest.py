"""Authenticated REST command adapter."""

from __future__ import annotations

from dataclasses import dataclass
from secrets import token_urlsafe
from typing import Any
from urllib.parse import quote
from uuid import UUID

from pydantic import SecretStr, ValidationError

from recap.adapter.http_transport import HTTPResult, HTTPTransport
from recap.adapter.transport import QueryRequest, QueryResult, hydrate_result
from recap.client.permissions import ActorPermissions
from recap.commands.models import (
    CommandContext,
    CommandModel,
    CreateNamespace,
    UpdateNamespace,
)
from recap.commands.registry import COMMAND_REGISTRY
from recap.dsl.query import QuerySpec, SchemaT
from recap.exceptions import RecapProtocolError
from recap.lifecycle import LifecycleStatus
from recap.schemas.namespace import NamespaceContext


@dataclass(frozen=True, slots=True)
class RESTResult:
    entity: Any
    etag: str | None
    request_id: str | None


class RESTAdapter:
    """Authenticated HTTP adapter for Recap command endpoints."""

    def __init__(
        self,
        base_url: str,
        api_key: str | SecretStr | None = None,
        *,
        timeout: float = 30.0,
        _transport: HTTPTransport | None = None,
    ) -> None:
        if _transport is None and api_key is None:
            raise TypeError("api_key is required when transport is not provided")
        self._base_url = base_url.rstrip("/")
        self._transport = (
            _transport if _transport is not None else HTTPTransport(api_key, timeout=timeout)
        )

    def __repr__(self) -> str:
        return f"RESTAdapter(base_url={self._base_url!r}, transport={self._transport!r})"

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> RESTAdapter:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        etag: str | None = None,
        idempotency_key: str | None = None,
        idempotent: bool = True,
        params: dict[str, str] | None = None,
    ) -> RESTResult:
        headers: dict[str, str] = {}
        if etag is not None:
            headers["If-Match"] = etag
        if idempotent and (idempotency_key is not None or method in {"PUT", "POST", "PATCH"}):
            headers["Idempotency-Key"] = idempotency_key or token_urlsafe(18)
        url = f"{self._base_url}{path}"
        request_kwargs = {"json": body, "headers": headers}
        if params is not None:
            request_kwargs["params"] = params
        response: HTTPResult = self._transport.request(method, url, **request_kwargs)
        return RESTResult(
            response.body,
            response.etag,
            response.request_id,
        )

    def create_namespace(
        self,
        path: str,
        metadata: dict[str, Any] | None,
        context: CommandContext,
    ) -> NamespaceContext:
        return self.execute(CreateNamespace(path=path, metadata=metadata), context)

    def update_namespace(
        self,
        namespace_id: UUID,
        expected_revision: int,
        metadata: dict[str, Any] | None,
        status: LifecycleStatus | None,
        context: CommandContext,
        *,
        etag: str | None = None,
    ) -> NamespaceContext:
        command = UpdateNamespace(
            namespace_id=namespace_id,
            expected_revision=expected_revision,
            metadata=metadata,
            status=status,
        )
        if etag is None:
            return self.execute(command, context)
        return self.execute(command, context, etag_override=etag)

    def list_child_namespaces(self, parent_path: str) -> list[str]:
        path = parent_path.strip("/")
        route = "/api/v1/namespaces/children"
        if path:
            route += f"/{path}"
        result = self._request("GET", route)
        return list(result.entity or [])

    def query(
        self, schema: type[SchemaT], spec: QuerySpec, *, namespace_path: str
    ) -> list[SchemaT]:
        request = QueryRequest.from_query(schema, spec, namespace_path=namespace_path)
        response = self._request(
            "POST", "/api/v1/query", body=request.model_dump(mode="json"), idempotent=False
        )
        try:
            result = QueryResult.model_validate(response.entity)
            return hydrate_result(schema, result)
        except RecapProtocolError:
            raise
        except (KeyError, TypeError, ValueError, ValidationError):
            raise RecapProtocolError(
                "Malformed REST query response", url=f"{self._base_url}/api/v1/query",
                request_id=response.request_id,
            ) from None

    def count(
        self, schema: type[SchemaT], spec: QuerySpec, *, namespace_path: str
    ) -> int:
        request = QueryRequest.from_query(schema, spec, namespace_path=namespace_path)
        response = self._request(
            "POST", "/api/v1/query/count", body=request.model_dump(mode="json"), idempotent=False
        )
        if not isinstance(response.entity, int) or isinstance(response.entity, bool):
            raise RecapProtocolError(
                "Malformed REST count response", url=f"{self._base_url}/api/v1/query/count",
                request_id=response.request_id,
            )
        return response.entity

    def permissions(self, namespace_path: str) -> ActorPermissions:
        response = self._request(
            "GET", "/api/v1/permissions", params={"namespace_path": namespace_path},
            idempotent=False,
        )
        try:
            return ActorPermissions.model_validate(response.entity)
        except (TypeError, ValueError, ValidationError):
            raise RecapProtocolError(
                "Malformed REST permissions response", url=f"{self._base_url}/api/v1/permissions",
                request_id=response.request_id,
            ) from None

    def get_namespace_context(self, path: str) -> NamespaceContext:
        encoded = "/".join(quote(segment, safe="") for segment in path.strip("/").split("/"))
        route = f"/api/v1/namespaces/context/{encoded}"
        response = self._request("GET", route, idempotent=False)
        try:
            context = NamespaceContext.model_validate(response.entity)
            if response.etag is not None:
                context = context.model_copy(update={"etag": response.etag})
            return context
        except (TypeError, ValueError, ValidationError):
            raise RecapProtocolError(
                "Malformed REST namespace context response", url=f"{self._base_url}{route}",
                request_id=response.request_id,
            ) from None

    def create(
        self,
        resource: str,
        namespace_path: str,
        body: dict[str, Any],
        *,
        idempotency_key=None,
    ):
        route = f"/api/v1/{resource}/{namespace_path.strip('/')}"
        return self._request(
            "POST",
            route,
            body=body,
            idempotency_key=idempotency_key,
        )

    def update(
        self,
        resource: str,
        entity_id: UUID,
        body: dict[str, Any],
        *,
        etag: str,
        idempotency_key=None,
    ):
        return self._request(
            "PATCH",
            f"/api/v1/{resource}/{entity_id}",
            body=body,
            etag=etag,
            idempotency_key=idempotency_key,
        )

    def copy_resource(
        self,
        source_resource_id: UUID,
        destination_namespace_path: str,
        *,
        changes=None,
        idempotency_key=None,
    ):
        return self._request(
            "POST",
            f"/api/v1/resources/{source_resource_id}/copies",
            body={
                "destination_namespace": destination_namespace_path.strip("/"),
                **(changes or {}),
            },
            idempotency_key=idempotency_key,
        )

    def _execute_registered(
        self, command: CommandModel, context, *, etag_override: str | None = None
    ) -> RESTResult:
        registration = COMMAND_REGISTRY.by_command(command)
        encoded = registration.encode_request(command)
        return self._request(
            encoded.method,
            encoded.path,
            body=encoded.body,
            etag=encoded.etag if etag_override is None else etag_override,
            idempotency_key=getattr(context, "idempotency_key", None),
        )

    def execute(
        self, command: CommandModel, context, *, etag_override: str | None = None
    ) -> Any:
        """Submit command DTO using its canonical REST route."""
        registration = COMMAND_REGISTRY.by_command(command)
        result = self._execute_registered(command, context, etag_override=etag_override)
        return registration.decode_response(result.entity, result.etag, command=command)
