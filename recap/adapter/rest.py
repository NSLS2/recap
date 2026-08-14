"""Authenticated REST command adapter."""

from __future__ import annotations

from dataclasses import dataclass
from secrets import token_urlsafe
from typing import Any
from uuid import UUID

from pydantic import SecretStr

from recap.adapter.http_transport import HTTPResult, HTTPTransport
from recap.commands.models import (
    CommandContext,
    CommandModel,
    CreateNamespace,
    UpdateNamespace,
)
from recap.commands.registry import COMMAND_REGISTRY
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
        self._transport = _transport or HTTPTransport(api_key, timeout=timeout)

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
    ) -> RESTResult:
        headers: dict[str, str] = {}
        if etag is not None:
            headers["If-Match"] = etag
        if idempotency_key is not None or method in {"PUT", "POST", "PATCH"}:
            headers["Idempotency-Key"] = idempotency_key or token_urlsafe(18)
        url = f"{self._base_url}{path}"
        response: HTTPResult = self._transport.request(
            method, url, json=body, headers=headers
        )
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
