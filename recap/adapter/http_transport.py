"""Shared authenticated HTTP transport."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx2 as httpx
from pydantic import SecretStr

from recap.exceptions import (
    RecapConnectionError,
    RecapProtocolError,
    error_from_code,
)


@dataclass(frozen=True, slots=True)
class HTTPResult:
    body: Any
    etag: str | None
    request_id: str | None


class HTTPTransport:
    def __init__(self, api_key: str | SecretStr | None, *, timeout: float = 30.0):
        self._api_key = api_key if isinstance(api_key, SecretStr) else SecretStr(api_key or "")
        self._client = httpx.Client(timeout=timeout)
        self._closed = False

    def __repr__(self) -> str:
        return f"{type(self).__name__}(api_key=SecretStr('**********'))"

    def _headers(self) -> dict[str, str]:
        value = self._api_key.get_secret_value()
        return {"Authorization": f"Apikey {value}"} if value else {}

    def redact(self, value: str) -> str:
        secret = self._api_key.get_secret_value()
        return value.replace(secret, "**********") if secret else value

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> HTTPResult:
        request_headers = dict(headers or {})
        request_headers.update(self._headers())
        try:
            response = self._client.request(
                method, url, json=json, headers=request_headers
            )
        except httpx.RequestError as exc:
            raise RecapConnectionError(
                url=self.redact(url), message=self.redact(str(exc))
            ) from None

        request_id = response.headers.get("X-Request-ID")
        if request_id is not None:
            request_id = self.redact(request_id)
        payload: Any = None
        if response.content:
            try:
                payload = response.json()
            except (TypeError, ValueError):
                if 200 <= response.status_code < 300:
                    raise RecapProtocolError(
                        "Malformed JSON response",
                        url=self.redact(url),
                        status_code=response.status_code,
                        request_id=request_id,
                    ) from None
                payload = None

        if not 200 <= response.status_code < 300:
            error = payload.get("error") if isinstance(payload, dict) else None
            if not isinstance(error, dict):
                raise error_from_code(
                    "request_error",
                    "Malformed error response",
                    url=self.redact(url),
                    status_code=response.status_code,
                    request_id=request_id,
                )
            code = error.get("code")
            message = error.get("message")
            envelope_request_id = error.get("request_id")
            if (
                not isinstance(code, str)
                or not code
                or not isinstance(message, str)
                or not message
                or (envelope_request_id is not None and not isinstance(envelope_request_id, str))
            ):
                raise error_from_code(
                    "request_error",
                    "Malformed error response",
                    url=self.redact(url),
                    status_code=response.status_code,
                    request_id=request_id,
                )
            raise error_from_code(
                code,
                self.redact(message),
                url=self.redact(url),
                status_code=response.status_code,
                request_id=request_id or self.redact(envelope_request_id),
            )

        return HTTPResult(
            payload,
            response.headers.get("ETag"),
            request_id,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._client.close()

    def __enter__(self) -> HTTPTransport:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
