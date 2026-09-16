from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from recap.commands import registry
from recap.commands.errors import CommandValidationError
from recap.commands.models import (
    CommandModel,
    CopyProcessRun,
    CopyResource,
    CreateNamespace,
    CreateProcessRun,
    CreateProcessTemplate,
    CreateResource,
    CreateResourceTemplate,
    SetLifecycleStatus,
    UpdateNamespace,
    UpdateProcessRun,
    UpdateProcessTemplate,
    UpdateResource,
    UpdateResourceTemplate,
)
from recap.commands.registry import COMMAND_REGISTRY
from recap.commands.service import CommandService
from recap.dsl.drafts import (
    ProcessRunDraft,
    ProcessTemplateDraft,
    ResourceTemplateDraft,
)
from recap.schemas.process import ProcessRunSchema, ProcessTemplateSchema
from recap.schemas.resource import (
    ResourceCopyOptions,
    ResourceSchema,
    ResourceTemplateSchema,
)
from recap.server.rest_models import (
    CopyProcessRunRequest,
    CopyResourceRequest,
    CreateNamespaceRequest,
    CreateResourceRequest,
    UpdateNamespaceRequest,
    UpdateProcessRunRequest,
    UpdateResourceRequest,
)


class UnregisteredCommand(CommandModel):
    marker: str


def test_update_resource_has_registration():
    registration = COMMAND_REGISTRY.by_type(UpdateResource)

    assert registration.name == "update_resource"
    assert registration.service_handler == "update_resource"
    assert registration.method == "PATCH"
    assert registration.route_template == "/api/v1/resources/{resource_id}"
    assert registration.request_model is UpdateResourceRequest


def test_pilot_command_registry_is_complete():
    expected = {
        CreateResource,
        UpdateResource,
        CopyResource,
        CopyProcessRun,
        CreateResourceTemplate,
        CreateProcessTemplate,
        CreateProcessRun,
        UpdateResourceTemplate,
        UpdateProcessTemplate,
        UpdateProcessRun,
        CreateNamespace,
        UpdateNamespace,
        SetLifecycleStatus,
    }

    COMMAND_REGISTRY.validate_complete(expected)


def test_registry_covers_all_command_types():
    expected = {
        CreateResource,
        UpdateResource,
        CopyResource,
        CopyProcessRun,
        CreateResourceTemplate,
        UpdateResourceTemplate,
        CreateProcessTemplate,
        UpdateProcessTemplate,
        CreateProcessRun,
        UpdateProcessRun,
        SetLifecycleStatus,
        CreateNamespace,
        UpdateNamespace,
    }

    assert {item.command_type for item in COMMAND_REGISTRY.all()} == expected
    COMMAND_REGISTRY.validate_complete(expected)


def test_lifecycle_registration_uses_polymorphic_response_decoder():
    registration = COMMAND_REGISTRY.by_type(SetLifecycleStatus)

    assert registration.name == "set_lifecycle_status"
    assert registration.service_handler == "set_lifecycle_status"
    assert registration.method == "POST"
    assert registration.route_template == "/api/v1/lifecycle/{object_type}/{object_id}"
    assert registration.decode_response is registry._decode_lifecycle_response


@pytest.mark.parametrize(
    "object_type, schema_name",
    [
        ("resource", "ResourceSchema"),
        ("resource_template", "ResourceTemplateSchema"),
        ("process_template", "ProcessTemplateSchema"),
        ("process_run", "ProcessRunSchema"),
    ],
)
def test_lifecycle_response_decoder_selects_schema(object_type, schema_name):
    command = SetLifecycleStatus(
        object_type=object_type, object_id=uuid4(), expected_revision=1, status="ACTIVE"
    )

    schema_module = {
        "ResourceSchema": "recap.commands.registry.ResourceSchema",
        "ResourceTemplateSchema": "recap.commands.registry.ResourceTemplateSchema",
        "ProcessTemplateSchema": "recap.commands.registry.ProcessTemplateSchema",
        "ProcessRunSchema": "recap.commands.registry.ProcessRunSchema",
    }[schema_name]
    with patch(f"{schema_module}.model_validate", return_value=object()) as validate:
        decoded = registry._decode_lifecycle_response({}, '"2"', command=command)

    assert decoded is not None
    validate.assert_called_once_with({})


def test_lifecycle_response_decoder_rejects_unknown_object_type():
    command = SetLifecycleStatus(
        object_type="unknown", object_id=uuid4(), expected_revision=1, status="ACTIVE"
    )

    with pytest.raises(TypeError, match="Unsupported lifecycle object"):
        registry._decode_lifecycle_response({}, None, command=command)


def test_pilot_command_registry_rejects_unexpected_registered_types():
    with pytest.raises(CommandValidationError, match="Unexpected command registrations"):
        COMMAND_REGISTRY.validate_complete({UpdateResource})


def test_unregistered_command_fails_loudly():
    with pytest.raises(CommandValidationError, match="No command registration"):
        COMMAND_REGISTRY.by_command(UnregisteredCommand(marker="missing"))


def test_service_dispatches_update_resource_via_registered_handler(monkeypatch):
    command = UpdateResource(resource_id=uuid4(), expected_revision=1, name="renamed")
    result = object()
    calls = []

    def handler(self, context, **kwargs):
        calls.append(kwargs)
        return result

    monkeypatch.setattr(CommandService, "update_resource", handler)

    returned = CommandService(None).execute(command, context=object())

    assert returned is result
    assert calls == [
        {
            "resource_id": command.resource_id,
            "expected_revision": 1,
            "name": "renamed",
            "properties": None,
        }
    ]


def test_namespace_commands_have_registrations_and_complete_expected_set():
    create = COMMAND_REGISTRY.by_type(CreateNamespace)
    update = COMMAND_REGISTRY.by_type(UpdateNamespace)

    assert (create.method, create.route_template, create.request_model) == (
        "PUT",
        "/api/v1/namespaces/{namespace_path:path}",
        CreateNamespaceRequest,
    )
    assert (update.method, update.route_template, update.request_model) == (
        "PATCH",
        "/api/v1/namespaces/{namespace_id}",
        UpdateNamespaceRequest,
    )
    COMMAND_REGISTRY.validate_complete(
        {
            CreateResource,
            UpdateResource,
            CopyResource,
            CopyProcessRun,
            CreateResourceTemplate,
            CreateProcessTemplate,
            CreateProcessRun,
            UpdateResourceTemplate,
        UpdateProcessTemplate,
        UpdateProcessRun,
        CreateNamespace,
        UpdateNamespace,
        SetLifecycleStatus,
        }
    )


def test_update_namespace_encoding_uses_if_match_and_excludes_revision():
    command = UpdateNamespace(
        namespace_id=uuid4(), expected_revision=4, metadata={"owner": "amx"}
    )

    encoded = COMMAND_REGISTRY.by_command(command).encode_request(command)

    assert encoded.method == "PATCH"
    assert encoded.path == f"/api/v1/namespaces/{command.namespace_id}"
    assert encoded.body == {"metadata": {"owner": "amx"}}
    assert encoded.etag == '"4"'


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"If-Match": '"4"'}, 4),
        ({"If-Match": 'W/"7"'}, 7),
    ],
)
def test_registry_revision_header_helper(headers, expected):
    assert registry._expected_revision(headers) == expected


@pytest.mark.parametrize(
    "headers",
    [{}, {"If-Match": ""}, {"If-Match": '"0"'}, {"If-Match": "invalid"}],
)
def test_registry_revision_header_helper_rejects_invalid_headers(headers):
    with pytest.raises(CommandValidationError):
        registry._expected_revision(headers)


def test_namespace_update_request_rejects_empty_changes():
    with pytest.raises(ValueError, match="Namespace update is empty"):
        UpdateNamespaceRequest()


def test_namespace_response_decoder_preserves_etag():
    registration = COMMAND_REGISTRY.by_type(UpdateNamespace)

    decoded = registration.decode_response(
        {
            "id": str(uuid4()),
            "path": "beamline/amx",
            "revision": 7,
            "status": "ACTIVE",
            "metadata": {},
            "create_date": "2026-01-01T00:00:00Z",
            "modified_date": "2026-01-01T00:00:00Z",
        },
        'W/"server-7"',
    )

    assert decoded.etag == 'W/"server-7"'


def test_resource_registry_covers_create_copy_and_templates():
    expected = {
        CreateResource: ("POST", "/api/v1/resources/{namespace_path:path}", CreateResourceRequest, ResourceSchema),
        CopyResource: ("POST", "/api/v1/resources/{source_resource_id}/copies", CopyResourceRequest, ResourceSchema),
        CreateResourceTemplate: ("POST", "/api/v1/resource-templates/{namespace_path:path}", ResourceTemplateDraft, ResourceTemplateSchema),
        UpdateResourceTemplate: ("PATCH", "/api/v1/resource-templates/{template_id}", ResourceTemplateDraft, ResourceTemplateSchema),
    }
    for command_type, values in expected.items():
        registration = COMMAND_REGISTRY.by_type(command_type)
        assert (registration.method, registration.route_template, registration.request_model) == values[:3]
        assert registration.decode_response is not None


def test_copy_resource_codec_preserves_default_options_on_wire():
    resource_id = uuid4()
    copy = CopyResource(
        source_resource_id=resource_id,
        destination_namespace_path="beamline/amx",
    )

    encoded = COMMAND_REGISTRY.by_command(copy).encode_request(copy)

    assert encoded.body == {
        "destination_namespace": "beamline/amx",
        "name": None,
        "parent_id": None,
        "changes": {"properties": {}},
    }


def test_copy_resource_codec_preserves_explicit_options_on_wire():
    resource_id = uuid4()
    copy = CopyResource(
        source_resource_id=resource_id,
        destination_namespace_path="beamline/amx",
        options=ResourceCopyOptions(
            name="copy", changes={"properties": {"group": {"value": 2}}}
        ),
    )
    encoded = COMMAND_REGISTRY.by_command(copy).encode_request(copy)
    assert encoded.body == {
        "destination_namespace": "beamline/amx",
        "name": "copy",
        "parent_id": None,
        "changes": {"properties": {"group": {"value": 2}}},
    }
    assert encoded.path == f"/api/v1/resources/{resource_id}/copies"


def test_copy_resource_codec_preserves_parent_id_on_wire():
    source_id = UUID("00000000-0000-0000-0000-000000000001")
    parent_id = UUID("00000000-0000-0000-0000-000000000002")
    command = CopyResource(
        source_resource_id=source_id,
        destination_namespace_path="beamline/amx",
        options=ResourceCopyOptions(parent_id=parent_id),
    )

    encoded = COMMAND_REGISTRY.by_command(command).encode_request(command)

    assert encoded.body["parent_id"] == str(parent_id)
    decoded = COMMAND_REGISTRY.by_command(command).decode_command(
        {"source_resource_id": str(source_id)},
        {},
        CopyResourceRequest.model_validate(encoded.body),
    )
    assert decoded.options.parent_id == parent_id


def test_resource_codecs_preserve_wire_shapes_and_revision_headers():
    resource_id = uuid4()
    template_id = uuid4()
    create = CreateResource(
        namespace_path="beamline/amx",
        name="sample",
        template_id=template_id,
        parent_id=resource_id,
        properties={"group": {"value": 2}},
    )
    encoded = COMMAND_REGISTRY.by_command(create).encode_request(create)
    assert encoded.body == {
        "name": "sample",
        "template_id": str(template_id),
        "parent_id": str(resource_id),
        "properties": {"group": {"value": 2}},
    }
    assert encoded.path == "/api/v1/resources/beamline/amx"


def test_resource_template_codecs_unwrap_draft_and_use_if_match():
    draft = ResourceTemplateDraft(name="sample", version="1", type_names=[])
    create = CreateResourceTemplate(namespace_path="beamline", draft=draft)
    encoded = COMMAND_REGISTRY.by_command(create).encode_request(create)
    assert encoded.body == {"name": "sample", "version": "1", "labels": [], "type_names": [], "property_groups": [], "children": []}
    assert encoded.path == "/api/v1/resource-templates/beamline"

    template_id = uuid4()
    update = UpdateResourceTemplate(template_id=template_id, expected_revision=3, draft=draft)
    encoded = COMMAND_REGISTRY.by_command(update).encode_request(update)
    assert encoded.body == draft.model_dump(mode="json")
    assert encoded.etag == '"3"'
    assert encoded.path == f"/api/v1/resource-templates/{template_id}"


def test_process_registry_has_exact_routes_request_models_and_response_schemas():
    expected = {
        CreateProcessTemplate: (
            "POST",
            "/api/v1/process-templates/{namespace_path:path}",
            ProcessTemplateDraft,
            ProcessTemplateSchema,
        ),
        UpdateProcessTemplate: (
            "PATCH",
            "/api/v1/process-templates/{template_id}",
            ProcessTemplateDraft,
            ProcessTemplateSchema,
        ),
        CreateProcessRun: (
            "POST",
            "/api/v1/process-runs/{namespace_path:path}",
            ProcessRunDraft,
            ProcessRunSchema,
        ),
        UpdateProcessRun: (
            "PATCH",
            "/api/v1/process-runs/{process_run_id}",
            UpdateProcessRunRequest,
            ProcessRunSchema,
        ),
        CopyProcessRun: (
            "POST",
            "/api/v1/process-runs/{source_process_run_id}/copies",
            CopyProcessRunRequest,
            ProcessRunSchema,
        ),
    }
    for command_type, values in expected.items():
        registration = COMMAND_REGISTRY.by_type(command_type)
        assert (
            registration.method,
            registration.route_template,
            registration.request_model,
        ) == values[:3]
        assert registration.decode_response is not None


def test_process_template_codecs_unwrap_draft_and_use_if_match():
    draft = ProcessTemplateDraft(name="screen", version="1")
    create = CreateProcessTemplate(namespace_path="beamline/amx", draft=draft)
    encoded = COMMAND_REGISTRY.by_command(create).encode_request(create)
    assert encoded.body == {"name": "screen", "version": "1", "labels": [], "resource_slots": [], "steps": []}
    assert encoded.path == "/api/v1/process-templates/beamline/amx"

    update = UpdateProcessTemplate(template_id=uuid4(), expected_revision=3, draft=draft)
    encoded = COMMAND_REGISTRY.by_command(update).encode_request(update)
    assert encoded.body == draft.model_dump(mode="json")
    assert encoded.etag == '"3"'
    assert encoded.path == f"/api/v1/process-templates/{update.template_id}"


def test_process_run_codecs_keep_revision_in_if_match_only():
    template_id = uuid4()
    draft = ProcessRunDraft(
        name="run-1", description="first", template_id=template_id
    )
    create = CreateProcessRun(namespace_path="beamline/amx", draft=draft)
    encoded = COMMAND_REGISTRY.by_command(create).encode_request(create)
    assert encoded.body == {
        "name": "run-1",
        "description": "first",
        "template_id": str(template_id),
        "assignments": {},
        "steps": {},
    }
    assert encoded.path == "/api/v1/process-runs/beamline/amx"

    update = UpdateProcessRun(
        process_run_id=uuid4(), expected_revision=4, description="finished"
    )
    encoded = COMMAND_REGISTRY.by_command(update).encode_request(update)
    assert encoded.body == {
        "description": "finished",
        "status": None,
        "assignments": None,
        "steps": None,
    }
    assert encoded.etag == '"4"'
    assert "expected_revision" not in encoded.body


def test_create_codecs_preserve_supplied_ids():
    process_template_id = uuid4()
    resource_template_id = uuid4()
    process_run_id = uuid4()
    process_template = CreateProcessTemplate(
        namespace_path="beamline",
        draft=ProcessTemplateDraft(id=process_template_id, name="pt", version="1"),
    )
    resource_template = CreateResourceTemplate(
        namespace_path="beamline",
        draft=ResourceTemplateDraft(
            id=resource_template_id, name="rt", version="1", type_names=[]
        ),
    )
    process_run = CreateProcessRun(
        namespace_path="beamline",
        draft=ProcessRunDraft(
            id=process_run_id, name="run", description="", template_id=uuid4()
        ),
    )

    assert COMMAND_REGISTRY.by_command(process_template).encode_request(
        process_template
    ).body["id"] == str(process_template_id)
    assert COMMAND_REGISTRY.by_command(resource_template).encode_request(
        resource_template
    ).body["id"] == str(resource_template_id)
    assert COMMAND_REGISTRY.by_command(process_run).encode_request(process_run).body[
        "id"
    ] == str(process_run_id)


def test_process_run_route_decoder_reads_if_match_and_forbids_unknown_body_fields():
    registration = COMMAND_REGISTRY.by_type(UpdateProcessRun)
    process_run_id = uuid4()
    command = registration.decode_command(
        {"process_run_id": process_run_id},
        {"If-Match": 'W/"7"'},
        UpdateProcessRunRequest(description="finished"),
    )
    assert command.process_run_id == process_run_id
    assert command.expected_revision == 7
    assert command.description == "finished"

    with pytest.raises(ValueError):
        UpdateProcessRunRequest.model_validate({"description": "x", "unknown": 1})


def test_service_dispatches_process_run_via_registered_handler(monkeypatch):
    command = UpdateProcessRun(process_run_id=uuid4(), expected_revision=1, description="x")
    result = object()
    calls = []

    def handler(self, context, **kwargs):
        calls.append(kwargs)
        return result

    monkeypatch.setattr(CommandService, "update_process_run", handler)
    assert CommandService(None).execute(command, context=object()) is result
    assert calls == [{
        "process_run_id": command.process_run_id,
        "expected_revision": 1,
        "description": "x",
        "status": None,
        "assignments": None,
        "steps": None,
    }]
