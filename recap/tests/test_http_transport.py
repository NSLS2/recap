import json
from unittest.mock import patch

import httpx2
import pytest
from pydantic import SecretStr

from recap.adapter.http_transport import HTTPTransport
from recap.exceptions import (
    RecapAuthenticationError,
    RecapConflictError,
    RecapConnectionError,
    RecapInternalError,
    RecapNotFoundError,
    RecapPermissionDeniedError,
    RecapProtocolError,
    RecapRequestError,
    RecapServiceUnavailableError,
    RecapValidationError,
)


def make_response(status, body=None, *, headers=None, content=None):
    if content is None:
        content = b"" if body is None else json.dumps(body).encode()
    return httpx2.Response(
        status,
        headers=headers,
        content=content,
        request=httpx2.Request("GET", "https://recap.example"),
    )


def test_transport_sends_auth_and_returns_metadata():
    transport = HTTPTransport("secret", timeout=12.5)
    response = make_response(
        200,
        {"value": 3},
        headers={"ETag": '"7"', "X-Request-ID": "request-7"},
    )

    with patch.object(transport._client, "request", return_value=response) as request:
        result = transport.request(
            "PATCH",
            "https://recap.example/api/v1/resources/1",
            json={"name": "sample"},
            headers={"If-Match": '"6"'},
        )

    assert result.body == {"value": 3}
    assert result.etag == '"7"'
    assert result.request_id == "request-7"
    assert request.call_args.kwargs["headers"] == {
        "Authorization": "Apikey secret",
        "If-Match": '"6"',
    }


def test_transport_uses_custom_timeout():
    transport = HTTPTransport("secret", timeout=12.5)

    assert transport._client.timeout.connect == 12.5


@pytest.mark.parametrize(
    "error",
    [
        httpx2.RequestError("secret"),
        httpx2.ConnectError("secret"),
        httpx2.TimeoutException("secret"),
    ],
)
def test_request_error_becomes_connection_error_without_secret(error):
    transport = HTTPTransport("secret")
    with patch.object(transport._client, "request", side_effect=error), pytest.raises(
        RecapConnectionError
    ) as caught:
        transport.request("GET", "https://recap.example")

    assert "secret" not in str(caught.value)
    assert caught.value.url == "https://recap.example"


def test_caller_authorization_cannot_override_transport_auth():
    transport = HTTPTransport("owned-secret")
    response = make_response(200, {"ok": True})

    with patch.object(transport._client, "request", return_value=response) as request:
        transport.request(
            "GET",
            "https://recap.example",
            headers={"Authorization": "Apikey caller-secret", "X-Test": "value"},
        )

    assert request.call_args.kwargs["headers"] == {
        "Authorization": "Apikey owned-secret",
        "X-Test": "value",
    }


def test_secret_is_absent_from_repr_and_protocol_errors():
    transport = HTTPTransport(SecretStr("secret"))
    response = make_response(200, content=b"secret malformed")

    with patch.object(transport._client, "request", return_value=response), pytest.raises(
        RecapProtocolError
    ) as caught:
        transport.request("GET", "https://recap.example")

    assert "secret" not in repr(transport)
    assert "secret" not in str(caught.value)


def test_close_is_idempotent():
    transport = HTTPTransport("secret")

    with patch.object(transport._client, "close") as close:
        transport.close()
        transport.close()

    close.assert_called_once_with()


@pytest.mark.parametrize(
    ("code", "error_type"),
    [
        ("authentication_required", RecapAuthenticationError),
        ("permission_denied", RecapPermissionDeniedError),
        ("not_found", RecapNotFoundError),
        ("validation_error", RecapValidationError),
        ("conflict", RecapConflictError),
        ("service_unavailable", RecapServiceUnavailableError),
        ("internal_error", RecapInternalError),
        ("request_error", RecapRequestError),
    ],
)
def test_status_error_uses_public_error_category(code, error_type):
    response = make_response(
        409,
        {"error": {"code": code, "message": "Conflict", "request_id": "request-9"}},
    )
    transport = HTTPTransport("secret")

    with patch.object(transport._client, "request", return_value=response), pytest.raises(
        error_type
    ) as caught:
        transport.request("GET", "https://recap.example")

    assert caught.value.status_code == 409
    assert caught.value.request_id == "request-9"


def test_header_request_id_wins_over_envelope():
    response = make_response(
        409,
        {"error": {"code": "conflict", "message": "Conflict", "request_id": "body-id"}},
        headers={"X-Request-ID": "header-id"},
    )
    transport = HTTPTransport(None)

    with patch.object(transport._client, "request", return_value=response), pytest.raises(
        RecapConflictError
    ) as caught:
        transport.request("GET", "https://recap.example")

    assert caught.value.request_id == "header-id"


def test_unknown_error_code_falls_back_to_request_error():
    response = make_response(
        418,
        {
            "error": {
                "code": "future_code",
                "message": "Unknown failure",
                "request_id": "body-id",
            }
        },
    )
    transport = HTTPTransport("secret")

    with patch.object(transport._client, "request", return_value=response), pytest.raises(
        RecapRequestError
    ) as caught:
        transport.request("GET", "https://recap.example")

    assert type(caught.value) is RecapRequestError


def test_external_error_fields_are_redacted_before_construction():
    secret = "secret"
    url = "https://secret.example/secret"
    response = make_response(
        409,
        {
            "error": {
                "code": "conflict",
                "message": "message contains secret",
                "request_id": "request-secret",
            }
        },
        headers={"X-Request-ID": "header-secret"},
    )
    transport = HTTPTransport(secret)

    with patch.object(transport._client, "request", return_value=response), pytest.raises(
        RecapConflictError
    ) as caught:
        transport.request("GET", url)

    assert caught.value.url == "https://**********.example/**********"
    assert caught.value.message == "message contains **********"
    assert caught.value.request_id == "header-**********"


@pytest.mark.parametrize("body", [None, {}, {"error": {}}, {"error": "bad"}, {"error": {"code": "conflict"}}])
def test_malformed_status_body_becomes_safe_request_error(body):
    response = make_response(500, body)
    transport = HTTPTransport("secret")

    with patch.object(transport._client, "request", return_value=response), pytest.raises(
        RecapRequestError
    ) as caught:
        transport.request("GET", "https://recap.example")

    assert caught.value.message == "Malformed error response"
    assert "secret" not in str(caught.value)


def test_malformed_success_json_becomes_protocol_error():
    response = make_response(200, content=b"not json secret")
    transport = HTTPTransport("secret")

    with patch.object(transport._client, "request", return_value=response), pytest.raises(
        RecapProtocolError
    ) as caught:
        transport.request("GET", "https://recap.example")

    assert "not json secret" not in str(caught.value)


def test_context_manager_closes_transport():
    with patch.object(httpx2.Client, "close") as close, HTTPTransport(None):
        pass

    close.assert_called_once_with()
