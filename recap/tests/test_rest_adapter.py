from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest


def _response(status=201, *, headers=None, body=None):
    response = MagicMock()
    response.status_code = status
    response.headers = headers or {}
    response.json.return_value = body or {}
    if status >= 400:
        import httpx2

        response.raise_for_status.side_effect = httpx2.HTTPStatusError(
            f"HTTP {status}", request=MagicMock(), response=response
        )
    return response


def _malformed_response():
    response = _response(status=422, body={"detail": "secret"})
    response.content = b"not-json"
    response.json.side_effect = ValueError("malformed response")
    return response


def _empty_response():
    response = _response(status=422)
    response.content = b""
    return response


def _unstructured_response():
    return _response(status=422, body={"detail": "secret", "token": "secret"})


def _non_dict_error_response():
    response = _response(status=422, body={"error": "secret"})
    response.content = b'{"error":"secret"}'
    return response


def test_create_namespace_sends_exact_authenticated_request_and_parses_etag():
    from recap.adapter.rest import RESTAdapter

    response = _response(
        headers={"ETag": '"7"', "X-Request-ID": "req-1"},
        body={"id": str(uuid4()), "path": "beamline/amx", "revision": 7},
    )
    with patch("recap.adapter.rest.httpx.Client") as client_type:
        client_type.return_value.request.return_value = response
        adapter = RESTAdapter("https://recap.test/", api_key="secret")

        result = adapter.create_namespace(
            "beamline/amx", {"beamline": "amx"}, idempotency_key="idem-1"
        )

    client_type.return_value.request.assert_called_once_with(
        "PUT",
        "https://recap.test/api/v1/namespaces/beamline/amx",
        headers={"Authorization": "Apikey secret", "Idempotency-Key": "idem-1"},
        json={"metadata": {"beamline": "amx"}},
    )
    assert result.etag == '"7"'
    assert result.request_id == "req-1"
    assert result.entity["revision"] == 7
    assert "secret" not in repr(adapter)


def test_update_and_copy_send_preconditions_and_destination_path():
    from recap.adapter.rest import RESTAdapter

    source = uuid4()
    responses = [
        _response(headers={"ETag": '"2"', "X-Request-ID": "update"}),
        _response(headers={"ETag": '"1"', "X-Request-ID": "copy"}),
    ]
    with patch("recap.adapter.rest.httpx.Client") as client_type:
        client_type.return_value.request.side_effect = responses
        adapter = RESTAdapter("https://recap.test", api_key="secret")
        adapter.update_namespace(uuid4(), {"metadata": {"owner": "amx"}}, etag='"1"')
        adapter.copy_resource(source, "beamline/amx", changes={"name": "copy"})

    calls = client_type.return_value.request.call_args_list
    assert calls[0].kwargs["headers"]["If-Match"] == '"1"'
    assert calls[0].kwargs["headers"]["Idempotency-Key"]
    assert calls[1].args[:2] == (
        "POST",
        f"https://recap.test/api/v1/resources/{source}/copies",
    )
    assert calls[1].kwargs["json"] == {
        "destination_namespace": "beamline/amx",
        "name": "copy",
    }
    assert calls[1].kwargs["headers"]["Idempotency-Key"]


def test_list_child_namespaces_uses_get_endpoint_without_write_headers():
    from recap.adapter.rest import RESTAdapter

    response = _response(body=["amx", "fmx"])
    with patch("recap.adapter.rest.httpx.Client") as client_type:
        client_type.return_value.request.return_value = response
        adapter = RESTAdapter("https://recap.test", api_key="secret")

        result = adapter.list_child_namespaces("beamline")

    client_type.return_value.request.assert_called_once_with(
        "GET",
        "https://recap.test/api/v1/namespaces/children/beamline",
        headers={"Authorization": "Apikey secret"},
        json=None,
    )
    assert result == ["amx", "fmx"]


@pytest.mark.parametrize("exc_type", ["connect", "timeout"])
def test_transport_failures_map_to_typed_connection_error_without_secret(exc_type):
    import httpx2

    from recap.adapter.rest import RESTAdapter
    from recap.exceptions import RecapConnectionError

    error = (
        httpx2.ConnectError("secret connection detail")
        if exc_type == "connect"
        else httpx2.TimeoutException("secret timeout")
    )
    with patch("recap.adapter.rest.httpx.Client") as client_type:
        client_type.return_value.request.side_effect = error
        with pytest.raises(RecapConnectionError) as caught:
            RESTAdapter("https://recap.test", api_key="secret").create_namespace("amx")
    assert caught.value.status_code is None
    assert "secret" not in str(caught.value)


def test_http_error_exposes_status_and_request_id_as_typed_error():
    from recap.adapter.rest import RESTAdapter
    from recap.exceptions import RecapHTTPError

    response = _response(status=409, headers={"X-Request-ID": "req-9"})
    with patch("recap.adapter.rest.httpx.Client") as client_type:
        client_type.return_value.request.return_value = response
        with pytest.raises(RecapHTTPError) as caught:
            RESTAdapter("https://recap.test", api_key="secret").create_namespace("amx")
    assert caught.value.status_code == 409
    assert caught.value.request_id == "req-9"
    assert "secret" not in str(caught.value)


def test_http_error_exposes_structured_validation_message():
    from recap.adapter.rest import RESTAdapter
    from recap.exceptions import RecapHTTPError

    response = _response(
        status=422,
        headers={"X-Request-ID": "req-validation"},
        body={"error": {"message": "name is required"}},
    )
    with patch("recap.adapter.rest.httpx.Client") as client_type:
        client_type.return_value.request.return_value = response
        with pytest.raises(RecapHTTPError) as caught:
            RESTAdapter("https://recap.test", api_key="secret").create_namespace("amx")

    assert caught.value.message == "name is required"
    assert "name is required" in str(caught.value)
    assert caught.value.url == "https://recap.test/api/v1/namespaces/amx"
    assert caught.value.status_code == 422
    assert caught.value.request_id == "req-validation"


@pytest.mark.parametrize(
    ("make_response", "expects_json"),
    [
        pytest.param(_malformed_response, True, id="malformed-json"),
        pytest.param(_empty_response, False, id="empty-body"),
        pytest.param(_unstructured_response, True, id="unstructured-object"),
        pytest.param(_non_dict_error_response, True, id="non-dict-error"),
    ],
)
def test_http_error_hides_unstructured_response_details(make_response, expects_json):
    from recap.adapter.rest import RESTAdapter
    from recap.exceptions import RecapHTTPError

    response = make_response()

    with patch("recap.adapter.rest.httpx.Client") as client_type:
        client_type.return_value.request.return_value = response
        with pytest.raises(RecapHTTPError) as caught:
            RESTAdapter("https://recap.test", api_key="secret").create_namespace("amx")

    assert caught.value.message is None
    assert str(caught.value) == (
        "Recap request failed at https://recap.test/api/v1/namespaces/amx (HTTP 422)"
    )
    assert "secret" not in str(caught.value)
    if expects_json:
        response.json.assert_called_once_with()
    else:
        response.json.assert_not_called()
