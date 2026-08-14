"""Authenticated REST command adapter."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from secrets import token_urlsafe
from typing import Any
from uuid import UUID

import httpx2 as httpx
from pydantic import SecretStr

from recap.commands.models import (
    CommandContext,
    CommandModel,
    CreateNamespace,
    UpdateNamespace,
)
from recap.commands.registry import COMMAND_REGISTRY
from recap.exceptions import RecapConnectionError, RecapHTTPError
from recap.lifecycle import LifecycleStatus
from recap.schemas.namespace import NamespaceContext


@dataclass(frozen=True, slots=True)
class RESTResult:
    entity: Any
    etag: str | None
    request_id: str | None


class _RedactedAuth:
    def __init__(self, api_key: str | SecretStr):
        self._value = (
            api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        )

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Apikey {self._value}"}

    def redact(self, value: str) -> str:
        return value.replace(self._value, "**********")

    def __repr__(self) -> str:
        return "_RedactedAuth(api_key=SecretStr('**********'))"


class RESTAdapter:
    """Authenticated HTTP adapter for Recap command endpoints."""

    def __init__(
        self, base_url: str, api_key: str | SecretStr, *, timeout: float = 30.0
    ):
        self._base_url = base_url.rstrip("/")
        self._auth = _RedactedAuth(api_key)
        self._client = httpx.Client(timeout=timeout)

    def __repr__(self) -> str:
        return f"RESTAdapter(base_url={self._base_url!r}, auth={self._auth!r})"

    def close(self) -> None:
        self._client.close()

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
        headers = self._auth.headers()
        if etag is not None:
            headers["If-Match"] = etag
        if idempotency_key is not None or method in {"PUT", "POST", "PATCH"}:
            headers["Idempotency-Key"] = idempotency_key or token_urlsafe(18)
        url = f"{self._base_url}{path}"
        try:
            response = self._client.request(method, url, headers=headers, json=body)
            response.raise_for_status()
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise RecapConnectionError(
                url, message=self._auth.redact(str(exc))
            ) from None
        except httpx.HTTPStatusError as exc:
            response = exc.response
            message = None
            payload: Any = None
            if response.content:
                with suppress(TypeError, ValueError):
                    payload = response.json()
            if isinstance(payload, dict):
                error = payload.get("error")
                candidate = error.get("message") if isinstance(error, dict) else None
                if isinstance(candidate, str) and candidate:
                    message = self._auth.redact(candidate)
            raise RecapHTTPError(
                url,
                response.status_code,
                response.headers.get("X-Request-ID"),
                message=message,
            ) from None
        return RESTResult(
            response.json() if response.content else None,
            response.headers.get("ETag"),
            response.headers.get("X-Request-ID"),
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
