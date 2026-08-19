from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

from recap.adapter.transport import _prepare_dynamic_models
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
from recap.dsl.drafts import (
    ProcessRunDraft,
    ProcessTemplateDraft,
    ResourceTemplateDraft,
)
from recap.lifecycle import LifecycleStatus
from recap.schemas.namespace import NamespaceContext, NamespaceSchema
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
    SetLifecycleStatusRequest,
    UpdateNamespaceRequest,
    UpdateProcessRunRequest,
    UpdateResourceRequest,
)

Method = Literal["POST", "PATCH", "PUT"]
_IF_MATCH = re.compile(r'(?:W/)?"?([1-9][0-9]*)"?')


def _body_data(body: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
    return body.model_dump() if isinstance(body, BaseModel) else dict(body)


def _expected_revision(headers: Mapping[str, Any]) -> int:
    if_match = headers.get("If-Match")
    if not isinstance(if_match, str):
        raise CommandValidationError("Missing If-Match header")
    match = _IF_MATCH.fullmatch(if_match.strip())
    if match is None:
        raise CommandValidationError("Invalid If-Match header")
    return int(match.group(1))


def _path_uuid(path_params: Mapping[str, Any], name: str) -> UUID:
    return UUID(str(path_params[name]))


def _revision_etag(revision: int) -> str:
    return f'"{revision}"'


def _draft(body: BaseModel, schema: type[BaseModel]) -> BaseModel:
    return body if isinstance(body, schema) else schema.model_validate(body)


@dataclass(frozen=True, slots=True)
class EncodedRequest:
    method: Method
    path: str
    body: dict[str, Any] | None
    etag: str | None


@dataclass(frozen=True, slots=True)
class CommandRegistration:
    command_type: type[CommandModel]
    name: str
    service_handler: str
    method: Method
    route_template: str
    request_model: type[BaseModel] | None
    encode_request: Callable[[CommandModel], EncodedRequest]
    decode_command: Callable[
        [Mapping[str, Any], Mapping[str, Any], BaseModel], CommandModel
    ]
    decode_response: Callable[..., Any]


class CommandRegistry:
    def __init__(self, registrations: Collection[CommandRegistration]) -> None:
        registrations = tuple(registrations)
        command_types = [registration.command_type for registration in registrations]
        names = [registration.name for registration in registrations]
        if len(set(command_types)) != len(command_types):
            raise ValueError("Duplicate command registration")
        if len(set(names)) != len(names):
            raise ValueError("Duplicate command registration name")
        self._registrations = registrations
        self._by_type = {registration.command_type: registration for registration in registrations}
        self._by_name = {registration.name: registration for registration in registrations}

    def by_type(self, command_type: type[CommandModel]) -> CommandRegistration:
        try:
            return self._by_type[command_type]
        except KeyError as exc:
            raise CommandValidationError(
                f"No command registration for {command_type.__name__}"
            ) from exc

    def by_command(self, command: CommandModel) -> CommandRegistration:
        return self.by_type(type(command))

    def by_name(self, name: str) -> CommandRegistration:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise CommandValidationError(f"No command registration named {name}") from exc

    def all(self) -> tuple[CommandRegistration, ...]:
        return self._registrations

    def validate_complete(self, command_types: Collection[type[CommandModel]]) -> None:
        expected = set(command_types)
        registered = set(self._by_type)
        missing = expected - registered
        unexpected = registered - expected
        if missing or unexpected:
            details = []
            if missing:
                names = ", ".join(sorted(command_type.__name__ for command_type in missing))
                details.append(f"Missing command registrations: {names}")
            if unexpected:
                names = ", ".join(
                    sorted(command_type.__name__ for command_type in unexpected)
                )
                details.append(f"Unexpected command registrations: {names}")
            raise CommandValidationError("; ".join(details))


def _encode_update_resource(command: CommandModel) -> EncodedRequest:
    assert isinstance(command, UpdateResource)
    data = command.model_dump(mode="json")
    return EncodedRequest(
        method="PATCH",
        path=f"/api/v1/resources/{command.resource_id}",
        body={
            key: value
            for key, value in data.items()
            if key not in {"resource_id", "expected_revision"}
        },
        etag=_revision_etag(command.expected_revision),
    )


def _decode_update_resource(
    path_params: Mapping[str, Any],
    headers: Mapping[str, Any],
    body: BaseModel,
) -> UpdateResource:
    data = _body_data(body)
    return UpdateResource(
        resource_id=_path_uuid(path_params, "resource_id"),
        expected_revision=_expected_revision(headers),
        **data,
    )


def _decode_resource_response(entity: Any, etag: str | None = None, **_: Any) -> ResourceSchema:
    if isinstance(entity, dict):
        _prepare_dynamic_models(entity)
    return ResourceSchema.model_validate(entity)


def _encode_create_resource(command: CommandModel) -> EncodedRequest:
    assert isinstance(command, CreateResource)
    data = command.model_dump(mode="json")
    data.pop("namespace_path")
    return EncodedRequest(
        method="POST",
        path=f"/api/v1/resources/{command.namespace_path.strip('/')}",
        body=data,
        etag=None,
    )


def _decode_create_resource(
    path_params: Mapping[str, Any], headers: Mapping[str, Any], body: BaseModel
) -> CreateResource:
    data = _body_data(body)
    data["template_id"] = UUID(str(data["template_id"]))
    if data.get("parent_id") is not None:
        data["parent_id"] = UUID(str(data["parent_id"]))
    return CreateResource(namespace_path=str(path_params["namespace_path"]), **data)


def _encode_copy_resource(command: CommandModel) -> EncodedRequest:
    assert isinstance(command, CopyResource)
    body = {
        "destination_namespace": command.destination_namespace_path.strip("/"),
        **command.options.model_dump(mode="json"),
    }
    return EncodedRequest(
        method="POST",
        path=f"/api/v1/resources/{command.source_resource_id}/copies",
        body=body,
        etag=None,
    )


def _decode_copy_resource(
    path_params: Mapping[str, Any], headers: Mapping[str, Any], body: BaseModel
) -> CopyResource:
    data = _body_data(body)
    destination = data.pop("destination_namespace")
    return CopyResource(
        source_resource_id=_path_uuid(path_params, "source_resource_id"),
        destination_namespace_path=destination,
        options=ResourceCopyOptions.model_validate(data),
    )


def _encode_copy_process_run(command: CommandModel) -> EncodedRequest:
    assert isinstance(command, CopyProcessRun)
    return EncodedRequest(
        method="POST",
        path=f"/api/v1/process-runs/{command.source_process_run_id}/copies",
        body={
            "destination_namespace": command.destination_namespace_path,
            **command.options.model_dump(mode="json"),
        },
        etag=None,
    )


def _decode_copy_process_run(
    path_params: Mapping[str, Any], headers: Mapping[str, Any], body: BaseModel
) -> CopyProcessRun:
    data = _body_data(body)
    destination = data.pop("destination_namespace")
    return CopyProcessRun(
        source_process_run_id=_path_uuid(path_params, "source_process_run_id"),
        destination_namespace_path=destination,
        options=data,
    )


def _encode_create_resource_template(command: CommandModel) -> EncodedRequest:
    assert isinstance(command, CreateResourceTemplate)
    return EncodedRequest(
        method="POST",
        path=f"/api/v1/resource-templates/{command.namespace_path.strip('/')}",
        body=command.draft.model_dump(mode="json"),
        etag=None,
    )


def _decode_create_resource_template(
    path_params: Mapping[str, Any], headers: Mapping[str, Any], body: BaseModel
) -> CreateResourceTemplate:
    draft = _draft(body, ResourceTemplateDraft)
    return CreateResourceTemplate(
        namespace_path=str(path_params["namespace_path"]), draft=draft
    )


def _encode_update_resource_template(command: CommandModel) -> EncodedRequest:
    assert isinstance(command, UpdateResourceTemplate)
    return EncodedRequest(
        method="PATCH",
        path=f"/api/v1/resource-templates/{command.template_id}",
        body=command.draft.model_dump(mode="json"),
        etag=_revision_etag(command.expected_revision),
    )


def _decode_update_resource_template(
    path_params: Mapping[str, Any], headers: Mapping[str, Any], body: BaseModel
) -> UpdateResourceTemplate:
    draft = _draft(body, ResourceTemplateDraft)
    return UpdateResourceTemplate(
        template_id=_path_uuid(path_params, "template_id"),
        expected_revision=_expected_revision(headers),
        draft=draft,
    )


def _decode_resource_template_response(entity: Any, etag: str | None = None, **_: Any) -> ResourceTemplateSchema:
    return ResourceTemplateSchema.model_validate(entity)


def _encode_create_process_template(command: CommandModel) -> EncodedRequest:
    assert isinstance(command, CreateProcessTemplate)
    return EncodedRequest(
        method="POST",
        path=f"/api/v1/process-templates/{command.namespace_path.strip('/')}",
        body=command.draft.model_dump(mode="json"),
        etag=None,
    )


def _decode_create_process_template(
    path_params: Mapping[str, Any], headers: Mapping[str, Any], body: BaseModel
) -> CreateProcessTemplate:
    draft = _draft(body, ProcessTemplateDraft)
    return CreateProcessTemplate(namespace_path=str(path_params["namespace_path"]), draft=draft)


def _encode_update_process_template(command: CommandModel) -> EncodedRequest:
    assert isinstance(command, UpdateProcessTemplate)
    return EncodedRequest(
        method="PATCH",
        path=f"/api/v1/process-templates/{command.template_id}",
        body=command.draft.model_dump(mode="json"),
        etag=_revision_etag(command.expected_revision),
    )


def _decode_update_process_template(
    path_params: Mapping[str, Any], headers: Mapping[str, Any], body: BaseModel
) -> UpdateProcessTemplate:
    draft = _draft(body, ProcessTemplateDraft)
    return UpdateProcessTemplate(
        template_id=_path_uuid(path_params, "template_id"),
        expected_revision=_expected_revision(headers),
        draft=draft,
    )


def _encode_create_process_run(command: CommandModel) -> EncodedRequest:
    assert isinstance(command, CreateProcessRun)
    return EncodedRequest(
        method="POST",
        path=f"/api/v1/process-runs/{command.namespace_path.strip('/')}",
        body=command.draft.model_dump(mode="json"),
        etag=None,
    )


def _decode_create_process_run(
    path_params: Mapping[str, Any], headers: Mapping[str, Any], body: BaseModel
) -> CreateProcessRun:
    draft = _draft(body, ProcessRunDraft)
    return CreateProcessRun(namespace_path=str(path_params["namespace_path"]), draft=draft)


def _encode_update_process_run(command: CommandModel) -> EncodedRequest:
    assert isinstance(command, UpdateProcessRun)
    data = command.model_dump(mode="json")
    data.pop("process_run_id")
    data.pop("expected_revision")
    return EncodedRequest(
        method="PATCH",
        path=f"/api/v1/process-runs/{command.process_run_id}",
        body=data,
        etag=_revision_etag(command.expected_revision),
    )


def _decode_update_process_run(
    path_params: Mapping[str, Any], headers: Mapping[str, Any], body: BaseModel
) -> UpdateProcessRun:
    data = _body_data(body)
    return UpdateProcessRun(
        process_run_id=_path_uuid(path_params, "process_run_id"),
        expected_revision=_expected_revision(headers),
        **data,
    )


def _decode_process_template_response(entity: Any, etag: str | None = None, **_: Any) -> ProcessTemplateSchema:
    return ProcessTemplateSchema.model_validate(entity)


def _decode_process_run_response(entity: Any, etag: str | None = None, **_: Any) -> ProcessRunSchema:
    return ProcessRunSchema.model_validate(entity)


def _encode_create_namespace(command: CommandModel) -> EncodedRequest:
    assert isinstance(command, CreateNamespace)
    return EncodedRequest(
        method="PUT",
        path=f"/api/v1/namespaces/{command.path.strip('/')}",
        body={"metadata": command.metadata or {}},
        etag=None,
    )


def _decode_create_namespace(
    path_params: Mapping[str, Any],
    headers: Mapping[str, Any],
    body: BaseModel,
) -> CreateNamespace:
    data = _body_data(body)
    return CreateNamespace(path=str(path_params["namespace_path"]), **data)


def _encode_update_namespace(command: CommandModel) -> EncodedRequest:
    assert isinstance(command, UpdateNamespace)
    data = command.model_dump(mode="json")
    return EncodedRequest(
        method="PATCH",
        path=f"/api/v1/namespaces/{command.namespace_id}",
        body={
            key: value
            for key, value in data.items()
            if key not in {"namespace_id", "expected_revision"} and value is not None
        },
        etag=_revision_etag(command.expected_revision),
    )


def _decode_update_namespace(
    path_params: Mapping[str, Any],
    headers: Mapping[str, Any],
    body: BaseModel,
) -> UpdateNamespace:
    data = _body_data(body)
    return UpdateNamespace(
        namespace_id=_path_uuid(path_params, "namespace_id"),
        expected_revision=_expected_revision(headers),
        **data,
    )


def _decode_namespace_response(
    entity: Any, etag: str | None = None, **_: Any
) -> NamespaceContext:
    namespace = NamespaceSchema.model_validate(entity)
    return NamespaceContext(
        id=namespace.id,
        path=namespace.path,
        metadata=namespace.metadata,
        status=namespace.status,
        revision=namespace.revision,
        etag=etag,
    )


def _encode_lifecycle_status(command: CommandModel) -> EncodedRequest:
    assert isinstance(command, SetLifecycleStatus)
    return EncodedRequest(
        method="POST",
        path=f"/api/v1/lifecycle/{command.object_type}/{command.object_id}",
        body={"status": command.status},
        etag=_revision_etag(command.expected_revision),
    )


def _decode_lifecycle_status(
    path_params: Mapping[str, Any],
    headers: Mapping[str, Any],
    body: BaseModel,
) -> SetLifecycleStatus:
    data = _body_data(body)
    return SetLifecycleStatus(
        object_type=str(path_params["object_type"]),
        object_id=_path_uuid(path_params, "object_id"),
        expected_revision=_expected_revision(headers),
        status=data["status"].value if isinstance(data["status"], LifecycleStatus) else data["status"],
    )


def _decode_lifecycle_response(
    entity: Any, etag: str | None = None, *, command: CommandModel
) -> Any:
    assert isinstance(command, SetLifecycleStatus)
    schemas = {
        "resource": ResourceSchema,
        "resource_template": ResourceTemplateSchema,
        "process_template": ProcessTemplateSchema,
        "process_run": ProcessRunSchema,
    }
    schema = schemas.get(command.object_type)
    if schema is None:
        raise TypeError(f"Unsupported lifecycle object: {command.object_type}")
    if isinstance(entity, dict) and schema is ResourceSchema:
        _prepare_dynamic_models(entity)
    return schema.model_validate(entity)


COMMAND_REGISTRY = CommandRegistry(
    (
        CommandRegistration(
            command_type=CreateResource,
            name="create_resource",
            service_handler="create_resource",
            method="POST",
            route_template="/api/v1/resources/{namespace_path:path}",
            request_model=CreateResourceRequest,
            encode_request=_encode_create_resource,
            decode_command=_decode_create_resource,
            decode_response=_decode_resource_response,
        ),
        CommandRegistration(
            command_type=UpdateResource,
            name="update_resource",
            service_handler="update_resource",
            method="PATCH",
            route_template="/api/v1/resources/{resource_id}",
            request_model=UpdateResourceRequest,
            encode_request=_encode_update_resource,
            decode_command=_decode_update_resource,
            decode_response=_decode_resource_response,
        ),
        CommandRegistration(
            command_type=CopyResource,
            name="copy_resource",
            service_handler="copy_resource",
            method="POST",
            route_template="/api/v1/resources/{source_resource_id}/copies",
            request_model=CopyResourceRequest,
            encode_request=_encode_copy_resource,
            decode_command=_decode_copy_resource,
            decode_response=_decode_resource_response,
        ),
        CommandRegistration(
            command_type=CopyProcessRun,
            name="copy_process_run",
            service_handler="copy_process_run",
            method="POST",
            route_template="/api/v1/process-runs/{source_process_run_id}/copies",
            request_model=CopyProcessRunRequest,
            encode_request=_encode_copy_process_run,
            decode_command=_decode_copy_process_run,
            decode_response=_decode_process_run_response,
        ),
        CommandRegistration(
            command_type=CreateResourceTemplate,
            name="create_resource_template",
            service_handler="create_resource_template",
            method="POST",
            route_template="/api/v1/resource-templates/{namespace_path:path}",
            request_model=ResourceTemplateDraft,
            encode_request=_encode_create_resource_template,
            decode_command=_decode_create_resource_template,
            decode_response=_decode_resource_template_response,
        ),
        CommandRegistration(
            command_type=UpdateResourceTemplate,
            name="update_resource_template",
            service_handler="update_resource_template",
            method="PATCH",
            route_template="/api/v1/resource-templates/{template_id}",
            request_model=ResourceTemplateDraft,
            encode_request=_encode_update_resource_template,
            decode_command=_decode_update_resource_template,
            decode_response=_decode_resource_template_response,
        ),
        CommandRegistration(
            command_type=CreateProcessTemplate,
            name="create_process_template",
            service_handler="create_process_template",
            method="POST",
            route_template="/api/v1/process-templates/{namespace_path:path}",
            request_model=ProcessTemplateDraft,
            encode_request=_encode_create_process_template,
            decode_command=_decode_create_process_template,
            decode_response=_decode_process_template_response,
        ),
        CommandRegistration(
            command_type=UpdateProcessTemplate,
            name="update_process_template",
            service_handler="update_process_template",
            method="PATCH",
            route_template="/api/v1/process-templates/{template_id}",
            request_model=ProcessTemplateDraft,
            encode_request=_encode_update_process_template,
            decode_command=_decode_update_process_template,
            decode_response=_decode_process_template_response,
        ),
        CommandRegistration(
            command_type=CreateProcessRun,
            name="create_process_run",
            service_handler="create_process_run",
            method="POST",
            route_template="/api/v1/process-runs/{namespace_path:path}",
            request_model=ProcessRunDraft,
            encode_request=_encode_create_process_run,
            decode_command=_decode_create_process_run,
            decode_response=_decode_process_run_response,
        ),
        CommandRegistration(
            command_type=UpdateProcessRun,
            name="update_process_run",
            service_handler="update_process_run",
            method="PATCH",
            route_template="/api/v1/process-runs/{process_run_id}",
            request_model=UpdateProcessRunRequest,
            encode_request=_encode_update_process_run,
            decode_command=_decode_update_process_run,
            decode_response=_decode_process_run_response,
        ),
        CommandRegistration(
            command_type=SetLifecycleStatus,
            name="set_lifecycle_status",
            service_handler="set_lifecycle_status",
            method="POST",
            route_template="/api/v1/lifecycle/{object_type}/{object_id}",
            request_model=SetLifecycleStatusRequest,
            encode_request=_encode_lifecycle_status,
            decode_command=_decode_lifecycle_status,
            decode_response=_decode_lifecycle_response,
        ),
        CommandRegistration(
            command_type=CreateNamespace,
            name="create_namespace",
            service_handler="create_namespace",
            method="PUT",
            route_template="/api/v1/namespaces/{namespace_path:path}",
            request_model=CreateNamespaceRequest,
            encode_request=_encode_create_namespace,
            decode_command=_decode_create_namespace,
            decode_response=_decode_namespace_response,
        ),
        CommandRegistration(
            command_type=UpdateNamespace,
            name="update_namespace",
            service_handler="update_namespace",
            method="PATCH",
            route_template="/api/v1/namespaces/{namespace_id}",
            request_model=UpdateNamespaceRequest,
            encode_request=_encode_update_namespace,
            decode_command=_decode_update_namespace,
            decode_response=_decode_namespace_response,
        ),
    )
)
