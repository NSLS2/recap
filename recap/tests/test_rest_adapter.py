from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from recap.adapter.http_transport import HTTPResult, HTTPTransport
from recap.adapter.transport import serialize_model


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


def test_namespace_methods_dispatch_through_registry_execute(monkeypatch):
    from recap.adapter.rest import RESTAdapter
    from recap.commands.models import CreateNamespace, UpdateNamespace

    adapter = RESTAdapter("https://recap.test", _transport=_transport())
    returned = object()
    commands = []

    def execute(command, context, **kwargs):
        commands.append(command)
        return returned

    monkeypatch.setattr(adapter, "execute", execute)
    context = SimpleNamespace(idempotency_key="idem-1")

    assert adapter.create_namespace("beamline", {"owner": "amx"}, context) is returned
    assert adapter.update_namespace(uuid4(), 2, {"owner": "fmx"}, None, context) is returned
    assert isinstance(commands[0], CreateNamespace)
    assert isinstance(commands[1], UpdateNamespace)


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


def test_close_delegates_to_transport():
    from recap.adapter.rest import RESTAdapter

    transport = _transport()
    RESTAdapter("https://recap.test", _transport=transport).close()

    transport.close.assert_called_once_with()
