from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx2
import pytest

from recap.adapter.http_transport import HTTPResult, HTTPTransport
from recap.adapter.transport import serialize_model
from recap.client.permissions import ActorPermissions
from recap.dsl.query import QuerySpec
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
    error_from_code,
)


def _transport(body=None, *, etag=None, request_id=None):
    transport = MagicMock(spec=HTTPTransport)
    transport.request.return_value = HTTPResult(body, etag, request_id)
    return transport


def _namespace_body(namespace_id=None):
    stamp = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
    return {
        "id": str(namespace_id or uuid4()),
        "path": "beamline/amx",
        "revision": 1,
        "status": "ACTIVE",
        "metadata": {},
        "create_date": stamp,
        "modified_date": stamp,
    }


def test_create_namespace_uses_registered_route_and_hydrates_response():
    from recap.adapter.rest import RESTAdapter
    from recap.schemas.namespace import NamespaceContext

    resource = NamespaceContext(id=uuid4(), path="beamline/amx", revision=7, metadata={"beamline": "amx"})
    transport = _transport(
        {**_namespace_body(resource.id), "revision": 7, "metadata": {"beamline": "amx"}},
        etag='"7"',
        request_id="req-1",
    )
    adapter = RESTAdapter("https://recap.test/", _transport=transport)

    result = adapter.create_namespace(
        "beamline/amx", {"beamline": "amx"}, SimpleNamespace(idempotency_key="idem-1")
    )

    transport.request.assert_called_once_with(
        "PUT",
        "https://recap.test/api/v1/namespaces/beamline/amx",
        json={"metadata": {"beamline": "amx"}},
        headers={"Idempotency-Key": "idem-1"},
    )
    assert isinstance(result, NamespaceContext)
    assert result.id == resource.id
    assert result.etag == '"7"'
    assert result.revision == resource.revision


def test_api_key_constructs_default_transport():
    from recap.adapter.rest import RESTAdapter

    with patch("recap.adapter.rest.HTTPTransport") as transport_type:
        adapter = RESTAdapter("https://recap.test", api_key="secret", timeout=12.5)

    transport_type.assert_called_once_with("secret", timeout=12.5)
    assert adapter._transport is transport_type.return_value


def test_missing_api_key_is_rejected_without_transport():
    from recap.adapter.rest import RESTAdapter

    with pytest.raises(TypeError, match="api_key is required"):
        RESTAdapter("https://recap.test")


def test_request_preserves_transport_request_id():
    from recap.adapter.rest import RESTAdapter

    adapter = RESTAdapter(
        "https://recap.test", _transport=_transport({"ok": True}, request_id="request-4")
    )

    result = adapter._request("GET", "/api/v1/status")

    assert result.request_id == "request-4"


def test_update_and_copy_preserve_if_match_routes_and_generated_idempotency_keys():
    from recap.adapter.rest import RESTAdapter

    source = uuid4()
    namespace_id = uuid4()
    transport = _transport(_namespace_body())
    adapter = RESTAdapter("https://recap.test", _transport=transport)

    adapter.update_namespace(
        namespace_id, 1, {"owner": "amx"}, None, SimpleNamespace(idempotency_key=None), etag='"1"'
    )
    adapter.copy_resource(source, "beamline/amx", changes={"name": "copy"})

    calls = transport.request.call_args_list
    assert calls[0].args[:2] == (
        "PATCH",
        f"https://recap.test/api/v1/namespaces/{namespace_id}",
    )
    assert calls[0].kwargs["headers"]["If-Match"] == '"1"'
    assert calls[0].kwargs["json"] == {"metadata": {"owner": "amx"}}
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


def test_update_namespace_preserves_explicit_empty_etag():
    from recap.adapter.rest import RESTAdapter

    transport = _transport(_namespace_body())
    RESTAdapter("https://recap.test", _transport=transport).update_namespace(
        uuid4(), 1, None, None, SimpleNamespace(idempotency_key=None), etag=""
    )

    assert transport.request.call_args.kwargs["headers"]["If-Match"] == ""


def test_execute_hydrates_copy_resource():
    from recap.adapter.rest import RESTAdapter
    from recap.commands.models import CopyResource
    from recap.schemas.resource import ResourceSchema

    source = uuid4()
    result = object()
    transport = _transport({"resource": "payload"})
    adapter = RESTAdapter("https://recap.test", _transport=transport)
    original = ResourceSchema.model_validate
    ResourceSchema.model_validate = classmethod(lambda cls, value: result)
    try:
        returned = adapter.execute(
            CopyResource(source_resource_id=source, destination_namespace_path="beamline/amx"),
            SimpleNamespace(idempotency_key="idem-copy"),
        )
    finally:
        ResourceSchema.model_validate = original

    assert returned is result
    transport.request.assert_called_once_with(
        "POST",
        f"https://recap.test/api/v1/resources/{source}/copies",
        json={"destination_namespace": "beamline/amx", "name": None, "changes": {"properties": {}}},
        headers={"Idempotency-Key": "idem-copy"},
    )


def test_execute_lifecycle_uses_status_route_and_if_match_header():
    from recap.adapter.rest import RESTAdapter
    from recap.commands.models import SetLifecycleStatus
    from recap.schemas.resource import ResourceTemplateSchema

    command = SetLifecycleStatus(
        object_type="resource_template", object_id=uuid4(), expected_revision=3, status="ACTIVE"
    )
    transport = _transport({"id": str(command.object_id), "revision": 4})
    original = ResourceTemplateSchema.model_validate
    ResourceTemplateSchema.model_validate = classmethod(lambda cls, value: object())
    try:
        RESTAdapter("https://recap.test", _transport=transport).execute(
            command, SimpleNamespace(idempotency_key="lifecycle-1")
        )
    finally:
        ResourceTemplateSchema.model_validate = original

    transport.request.assert_called_once_with(
        "POST",
        f"https://recap.test/api/v1/lifecycle/resource_template/{command.object_id}",
        json={"status": "ACTIVE"},
        headers={"If-Match": '"3"', "Idempotency-Key": "lifecycle-1"},
    )


def test_execute_update_resource_hydrates_dynamic_properties():
    from recap.adapter.rest import RESTAdapter
    from recap.commands.models import UpdateResource
    from recap.tests.transport_factories import full_resource

    resource = full_resource()
    command = UpdateResource(resource_id=resource.id, expected_revision=1, name=resource.name)
    transport = _transport(serialize_model(resource))

    returned = RESTAdapter("https://recap.test", _transport=transport).execute(
        command, SimpleNamespace(idempotency_key="resource-update")
    )

    assert returned.properties.measurements.values.captured_at.value == resource.properties.measurements.values.captured_at.value
    assert transport.request.call_args.kwargs["headers"] == {
        "If-Match": '"1"',
        "Idempotency-Key": "resource-update",
    }


def test_execute_update_resource_keeps_revision_out_of_request_body():
    from recap.adapter.rest import RESTAdapter
    from recap.commands.models import UpdateResource
    from recap.tests.transport_factories import minimal_resource

    resource = minimal_resource()
    command = UpdateResource(resource_id=resource.id, expected_revision=1, name="renamed")
    transport = _transport(serialize_model(resource))

    RESTAdapter("https://recap.test", _transport=transport).execute(
        command, SimpleNamespace(idempotency_key="resource-update")
    )

    request = transport.request.call_args
    assert request.args[:2] == (
        "PATCH",
        f"https://recap.test/api/v1/resources/{resource.id}",
    )
    assert request.kwargs["json"] == {"name": "renamed", "properties": None}
    assert "expected_revision" not in request.kwargs["json"]


def test_execute_update_process_run_uses_route_body_and_if_match():
    from recap.adapter.rest import RESTAdapter
    from recap.commands.models import UpdateProcessRun
    from recap.schemas.process import ProcessRunSchema

    command = UpdateProcessRun(
        process_run_id=uuid4(),
        expected_revision=1,
        description="finished",
        status="ACTIVE",
    )
    transport = _transport({"id": str(command.process_run_id), "revision": 2})
    original = ProcessRunSchema.model_validate
    ProcessRunSchema.model_validate = classmethod(lambda cls, value: object())
    try:
        RESTAdapter("https://recap.test", _transport=transport).execute(
            command, SimpleNamespace(idempotency_key="process-run-update")
        )
    finally:
        ProcessRunSchema.model_validate = original

    transport.request.assert_called_once_with(
        "PATCH",
        f"https://recap.test/api/v1/process-runs/{command.process_run_id}",
        json={
            "description": "finished",
            "status": "ACTIVE",
            "assignments": None,
            "steps": None,
        },
        headers={"If-Match": '"1"', "Idempotency-Key": "process-run-update"},
    )


def test_list_child_namespaces_uses_get_without_write_headers():
    from recap.adapter.rest import RESTAdapter

    transport = _transport(["amx", "fmx"])
    result = RESTAdapter("https://recap.test", _transport=transport).list_child_namespaces("beamline")

    transport.request.assert_called_once_with(
        "GET",
        "https://recap.test/api/v1/namespaces/children/beamline",
        json=None,
        headers={},
    )
    assert result == ["amx", "fmx"]


def test_read_operations_send_wire_payloads_and_no_idempotency_headers():
    from recap.adapter.rest import RESTAdapter
    from recap.schemas.resource import ResourceSchema

    permissions = {
        "identities": [],
        "snapshot_generation": None,
        "effective_scopes": ["namespace:read"],
        "matched_namespace_paths": ["beamline/amx"],
        "groups": [],
        "roles": [],
    }
    transport = _transport(
        {"entity": "resource", "projection": "full", "items": []}
    )
    transport.request.side_effect = [
        HTTPResult({"entity": "resource", "projection": "full", "items": []}, None, None),
        HTTPResult(4, None, None),
        HTTPResult(permissions, None, None),
        HTTPResult(_namespace_body(), '"1"', None),
    ]
    adapter = RESTAdapter("https://recap.test", _transport=transport)

    assert adapter.query(
        ResourceSchema, QuerySpec(filters={"name": "sample"}), namespace_path="beamline/amx"
    ) == []
    assert adapter.count(ResourceSchema, QuerySpec(), namespace_path="beamline/amx") == 4
    assert isinstance(adapter.permissions("beamline/amx"), ActorPermissions)
    adapter.get_namespace_context("beamline/amx station")

    query_call, count_call, permissions_call, context_call = transport.request.call_args_list
    assert query_call.kwargs["json"]["namespace_path"] == "beamline/amx"
    assert query_call.kwargs["json"]["entity"] == "resource"
    assert query_call.kwargs["json"]["projection"] == "full"
    assert query_call.kwargs["json"]["spec"]["filters"] == {"name": "sample"}
    assert count_call.kwargs["json"]["namespace_path"] == "beamline/amx"
    assert count_call.kwargs["json"]["entity"] == "resource"
    assert count_call.kwargs["json"]["spec"]["filters"] == {}
    assert permissions_call.kwargs["params"] == {"namespace_path": "beamline/amx"}
    assert context_call.args[:2] == (
        "GET",
        "https://recap.test/api/v1/namespaces/context/beamline/amx%20station",
    )
    assert context_call.kwargs["json"] is None
    for call in transport.request.call_args_list:
        assert "Idempotency-Key" not in call.kwargs["headers"]


@pytest.mark.parametrize(
    ("operation", "body", "message"),
    [
        ("query", None, "Malformed REST query response"),
        ("count", True, "Malformed REST count response"),
        ("permissions", {}, "Malformed REST permissions response"),
        ("context", {}, "Malformed REST namespace context response"),
    ],
)
def test_read_operations_reject_malformed_success_bodies(operation, body, message):
    from recap.adapter.rest import RESTAdapter
    from recap.schemas.resource import ResourceSchema

    adapter = RESTAdapter("https://recap.test", _transport=_transport(body))
    with pytest.raises(RecapProtocolError, match=message):
        if operation == "query":
            adapter.query(ResourceSchema, QuerySpec(), namespace_path="beamline/amx")
        elif operation == "count":
            adapter.count(ResourceSchema, QuerySpec(), namespace_path="beamline/amx")
        elif operation == "permissions":
            adapter.permissions("beamline/amx")
        else:
            adapter.get_namespace_context("beamline/amx")


def test_query_rejects_schema_mismatch_in_success_body():
    from recap.adapter.rest import RESTAdapter
    from recap.schemas.resource import ResourceSchema

    transport = _transport({"entity": "process_run", "projection": "full", "items": []})
    adapter = RESTAdapter("https://recap.test", _transport=transport)

    with pytest.raises(RecapProtocolError, match="schema does not match"):
        adapter.query(ResourceSchema, QuerySpec(), namespace_path="beamline/amx")


def test_close_delegates_to_transport():
    from recap.adapter.rest import RESTAdapter

    transport = _transport()
    RESTAdapter("https://recap.test", _transport=transport).close()

    transport.close.assert_called_once_with()


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
def test_rest_propagates_normalized_error_metadata(code, error_type):
    from recap.adapter.rest import RESTAdapter

    transport = _transport()
    transport.request.side_effect = error_from_code(
        code,
        "Safe message",
        url="https://recap.test/api/v1/resources",
        status_code=409,
        request_id="request-7",
    )

    with pytest.raises(error_type) as caught:
        RESTAdapter("https://recap.test", _transport=transport).list_child_namespaces("beamline")

    error = caught.value
    assert error.code == code
    assert error.message == "Safe message"
    assert error.url == "https://recap.test/api/v1/resources"
    assert error.status_code == 409
    assert error.request_id == "request-7"
    assert "secret" not in str(error)
    assert "raw internal" not in str(error)


@pytest.mark.parametrize(
    ("status_code", "response_code", "expected_code", "error_type"),
    [
        (401, "authentication_required", "authentication_required", RecapAuthenticationError),
        (403, "permission_denied", "permission_denied", RecapPermissionDeniedError),
        (404, "not_found", "not_found", RecapNotFoundError),
        (422, "validation_error", "validation_error", RecapValidationError),
        (409, "conflict", "conflict", RecapConflictError),
        (503, "service_unavailable", "service_unavailable", RecapServiceUnavailableError),
        (500, "internal_error", "internal_error", RecapInternalError),
        (418, "future_error", "request_error", RecapRequestError),
    ],
)
def test_rest_classifies_real_http_error_responses(
    status_code, response_code, expected_code, error_type
):
    from recap.adapter.rest import RESTAdapter

    transport = HTTPTransport("secret")
    response = httpx2.Response(
        status_code,
        json={
            "error": {
                "code": response_code,
                "message": "Safe message",
                "request_id": "request-8",
            }
        },
        headers={"X-Request-ID": "request-8"},
        request=httpx2.Request(
            "GET", "https://recap.test/api/v1/namespaces/children/beamline"
        ),
    )
    with patch.object(transport._client, "request", return_value=response), pytest.raises(
        error_type
    ) as caught:
        RESTAdapter("https://recap.test", _transport=transport).list_child_namespaces(
            "beamline"
        )

    assert caught.value.code == expected_code
    assert caught.value.message == "Safe message"
    assert caught.value.status_code == status_code
    assert caught.value.request_id == "request-8"
    assert caught.value.url.endswith("/api/v1/namespaces/children/beamline")
    assert "secret" not in str(caught.value)
    assert "raw internal" not in str(caught.value)


def test_rest_classifies_real_transport_failure():
    from recap.adapter.rest import RESTAdapter

    transport = HTTPTransport("secret")
    with patch.object(
        transport._client,
        "request",
        side_effect=httpx2.ConnectError("secret connection failure"),
    ), pytest.raises(RecapConnectionError) as caught:
        RESTAdapter("https://recap.test", _transport=transport).list_child_namespaces(
            "beamline"
        )

    assert caught.value.code == "connection_error"
    assert caught.value.url.endswith("/api/v1/namespaces/children/beamline")
    assert "secret" not in str(caught.value)
