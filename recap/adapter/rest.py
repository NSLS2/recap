"""Authenticated REST command adapter."""

from __future__ import annotations

from dataclasses import dataclass
from secrets import token_urlsafe
from typing import Any
from uuid import UUID

import httpx2 as httpx
from pydantic import SecretStr

from recap.commands.models import (
    CommandModel,
    CreateProcessTemplate,
    CreateResource,
    CreateResourceTemplate,
    UpdateProcessTemplate,
    UpdateResourceTemplate,
)
from recap.exceptions import RecapConnectionError, RecapHTTPError


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
            raise RecapHTTPError(
                url, response.status_code, response.headers.get("X-Request-ID")
            ) from None
        return RESTResult(
            response.json() if response.content else None,
            response.headers.get("ETag"),
            response.headers.get("X-Request-ID"),
        )

    def create_namespace(
        self, path: str, metadata: dict[str, Any] | None = None, *, idempotency_key=None
    ):
        return self._request(
            "PUT",
            f"/api/v1/namespaces/{path.strip('/')}",
            body={"metadata": metadata or {}},
            idempotency_key=idempotency_key,
        )

    def update_namespace(
        self,
        namespace_id: UUID,
        body: dict[str, Any],
        *,
        etag: str,
        idempotency_key=None,
    ):
        return self._request(
            "PATCH",
            f"/api/v1/namespaces/{namespace_id}",
            body=body,
            etag=etag,
            idempotency_key=idempotency_key,
        )

    def create(
        self,
        resource: str,
        namespace_path: str,
        body: dict[str, Any],
        *,
        idempotency_key=None,
    ):
        return self._request(
            "POST",
            f"/api/v1/{resource}/{namespace_path.strip('/')}",
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
            f"/api/v1/resources/{source_resource_id}/copies/{destination_namespace_path.strip('/')}",
            body=changes or {},
            idempotency_key=idempotency_key,
        )

    def execute(self, command: CommandModel, context) -> Any:
        """Submit command DTO using its canonical REST route."""
        data = command.model_dump(mode="json")
        key = getattr(context, "idempotency_key", None)
        if isinstance(command, CreateProcessTemplate):
            return self.create(
                "process-templates",
                command.namespace_path,
                data["draft"],
                idempotency_key=key,
            ).entity
        if isinstance(command, CreateResourceTemplate):
            return self.create(
                "resource-templates",
                command.namespace_path,
                data["draft"],
                idempotency_key=key,
            ).entity
        if isinstance(command, CreateResource):
            return self.create(
                "resources", command.namespace_path, data, idempotency_key=key
            ).entity
        if isinstance(command, UpdateProcessTemplate | UpdateResourceTemplate):
            resource = (
                "process-templates"
                if isinstance(command, UpdateProcessTemplate)
                else "resource-templates"
            )
            return self.update(
                resource,
                command.template_id,
                data["draft"],
                etag=f'"{command.expected_revision}"',
                idempotency_key=key,
            ).entity
        raise TypeError(f"Unsupported REST command: {type(command).__name__}")
