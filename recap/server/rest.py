from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response

from recap.adapter.local import LocalBackend
from recap.adapter.transport import QueryRequest, QueryResult, serialize_model
from recap.authentication.models import RequestActor
from recap.authorization.policy import (
    SnapshotNamespacePolicy,
    UnrestrictedNamespacePolicy,
)
from recap.client.permissions import ActorPermissions
from recap.commands.context import DiscardAuditSink
from recap.commands.models import CommandContext
from recap.commands.registry import COMMAND_REGISTRY, CommandRegistration
from recap.commands.service import CommandService
from recap.dsl.drafts import (
    ProcessRunDraft,
    ProcessTemplateDraft,
    ResourceTemplateDraft,
)
from recap.exceptions import AuthorizationDenied, RecapValidationError
from recap.schemas.namespace import NamespaceContext, NamespaceSchema
from recap.schemas.process import ProcessRunSchema, ProcessTemplateSchema
from recap.schemas.resource import (
    ResourceSchema,
    ResourceTemplateSchema,
)
from recap.server.errors import request_id_from
from recap.server.query_models import QueryRPCRequest
from recap.server.query_service import QueryService
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
from recap.server.security import authenticate_request
from recap.utils.namespace import canonicalize_namespace_path

router = APIRouter(prefix="/api/v1")


def command_registration(name: str):
    def resolve() -> CommandRegistration:
        return COMMAND_REGISTRY.by_name(name)

    return resolve


def command_service(request: Request) -> CommandService:
    return CommandService(request.app.state.session_factory)


def _context(request: Request, actor: RequestActor, idempotency_key: str):
    return CommandContext(
        actor=actor,
        request_id=request_id_from(request),
        policy=_policy(request),
        audit_sink=DiscardAuditSink(),
        authorization_generation=None,
        idempotency_key=idempotency_key,
    )


def _policy(request: Request):
    provider = getattr(request.app.state, "authorization_snapshot_provider", None)
    return (
        SnapshotNamespacePolicy(provider.acquire())
        if provider is not None
        else getattr(
            request.app.state, "namespace_policy", UnrestrictedNamespacePolicy()
        )
    )


def _set_etag(response: Response, entity):
    response.headers["ETag"] = f'"{entity.revision}"'
    return serialize_model(entity) if isinstance(entity, ResourceSchema) else entity


def _query_service(request: Request) -> QueryService:
    return QueryService(LocalBackend(request.app.state.session_factory))


def _canonical_namespace_path(path: str) -> str:
    try:
        return canonicalize_namespace_path(path)
    except (TypeError, ValueError) as exc:
        raise RecapValidationError("Invalid namespace path") from exc


def _query_request(body: QueryRPCRequest) -> QueryRequest:
    try:
        return QueryRequest(
            entity=body.entity,
            projection=body.projection,
            namespace_path=_canonical_namespace_path(body.namespace_path),
            spec=body.spec,
        )
    except (TypeError, ValueError) as exc:
        raise RecapValidationError("Invalid query request") from exc


@router.post("/query", response_model=QueryResult)
def query(
    body: QueryRPCRequest,
    request: Request,
    actor: Annotated[RequestActor, Depends(authenticate_request)],
) -> QueryResult:
    return _query_service(request).query(
        _query_request(body), actor=actor, policy=_policy(request)
    )


@router.post("/query/count")
def query_count(
    body: QueryRPCRequest,
    request: Request,
    actor: Annotated[RequestActor, Depends(authenticate_request)],
) -> int:
    return _query_service(request).count(
        _query_request(body), actor=actor, policy=_policy(request)
    )


@router.get("/permissions", response_model=ActorPermissions)
def permissions(
    request: Request,
    namespace_path: str,
    actor: Annotated[RequestActor, Depends(authenticate_request)],
) -> ActorPermissions:
    return _query_service(request).permissions(
        _canonical_namespace_path(namespace_path), actor=actor, policy=_policy(request)
    )


@router.get(
    "/namespaces/context/{namespace_path:path}", response_model=NamespaceContext
)
def namespace_context(
    namespace_path: str,
    request: Request,
    response: Response,
    actor: Annotated[RequestActor, Depends(authenticate_request)],
) -> NamespaceContext:
    path = _canonical_namespace_path(namespace_path)
    if not _policy(request).can_discover_namespace(actor, path):
        raise AuthorizationDenied(conceal=True)
    context = _query_service(request).backend.get_namespace_context(
        path
    )
    response.headers["ETag"] = f'"{context.revision}"'
    return context


@router.put(
    "/namespaces/{namespace_path:path}",
    response_model=NamespaceSchema,
    status_code=201,
)
def create_namespace(
    namespace_path: str,
    body: CreateNamespaceRequest,
    request: Request,
    response: Response,
    actor: Annotated[RequestActor, Depends(authenticate_request)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    registration: Annotated[
        CommandRegistration, Depends(command_registration("create_namespace"))
    ],
) -> NamespaceSchema:
    command = registration.decode_command(
        {"namespace_path": namespace_path}, {}, body
    )
    result = CommandService(request.app.state.session_factory).execute(
        command, _context(request, actor, idempotency_key)
    )
    return _set_etag(response, result)


@router.patch("/namespaces/{namespace_id}", response_model=NamespaceSchema)
def update_namespace(
    namespace_id: UUID,
    body: UpdateNamespaceRequest,
    request: Request,
    response: Response,
    actor: Annotated[RequestActor, Depends(authenticate_request)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    if_match: Annotated[str, Header(alias="If-Match")],
    registration: Annotated[
        CommandRegistration, Depends(command_registration("update_namespace"))
    ],
) -> NamespaceSchema:
    command = registration.decode_command(
        {"namespace_id": namespace_id}, {"If-Match": if_match}, body
    )
    result = CommandService(request.app.state.session_factory).execute(
        command, _context(request, actor, idempotency_key)
    )
    return _set_etag(response, result)


@router.get("/namespaces/children", response_model=list[str])
@router.get("/namespaces/children/{namespace_path:path}", response_model=list[str])
def list_child_namespaces(
    request: Request,
    actor: Annotated[RequestActor, Depends(authenticate_request)],
    namespace_path: str = "",
) -> list[str]:
    path = canonicalize_namespace_path(namespace_path)
    provider = getattr(request.app.state, "authorization_snapshot_provider", None)
    policy = (
        SnapshotNamespacePolicy(provider.acquire())
        if provider is not None
        else getattr(
            request.app.state, "namespace_policy", UnrestrictedNamespacePolicy()
        )
    )
    backend = LocalBackend(request.app.state.session_factory)
    child_paths = backend.list_child_namespace_paths(path)
    visible_paths = [
        child_path
        for child_path in child_paths
        if policy.can_discover_namespace(actor, child_path)
    ]
    prefix = f"{path}/" if path else ""
    return [child_path[len(prefix) :] for child_path in visible_paths]


@router.post(
    "/process-templates/{namespace_path:path}",
    response_model=ProcessTemplateSchema,
    status_code=201,
)
def create_process_template(
    namespace_path: str,
    body: ProcessTemplateDraft,
    request: Request,
    response: Response,
    actor: Annotated[RequestActor, Depends(authenticate_request)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    registration: Annotated[
        CommandRegistration, Depends(command_registration("create_process_template"))
    ],
) -> ProcessTemplateSchema:
    command = registration.decode_command(
        {"namespace_path": namespace_path}, {}, body
    )
    result = CommandService(request.app.state.session_factory).execute(
        command, _context(request, actor, idempotency_key)
    )
    return _set_etag(response, result)


@router.patch("/process-templates/{template_id}", response_model=ProcessTemplateSchema)
def update_process_template(
    template_id: UUID,
    body: ProcessTemplateDraft,
    request: Request,
    response: Response,
    actor: Annotated[RequestActor, Depends(authenticate_request)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    if_match: Annotated[str, Header(alias="If-Match")],
    registration: Annotated[
        CommandRegistration, Depends(command_registration("update_process_template"))
    ],
) -> ProcessTemplateSchema:
    command = registration.decode_command(
        {"template_id": template_id}, {"If-Match": if_match}, body
    )
    result = CommandService(request.app.state.session_factory).execute(
        command, _context(request, actor, idempotency_key)
    )
    return _set_etag(response, result)


@router.post(
    "/resource-templates/{namespace_path:path}",
    response_model=ResourceTemplateSchema,
    status_code=201,
)
def create_resource_template(
    namespace_path: str,
    body: ResourceTemplateDraft,
    request: Request,
    response: Response,
    actor: Annotated[RequestActor, Depends(authenticate_request)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    registration: Annotated[
        CommandRegistration, Depends(command_registration("create_resource_template"))
    ],
) -> ResourceTemplateSchema:
    command = registration.decode_command(
        {"namespace_path": namespace_path}, {}, body
    )
    result = CommandService(request.app.state.session_factory).execute(
        command, _context(request, actor, idempotency_key)
    )
    return _set_etag(response, result)


@router.patch(
    "/resource-templates/{template_id}", response_model=ResourceTemplateSchema
)
def update_resource_template(
    template_id: UUID,
    body: ResourceTemplateDraft,
    request: Request,
    response: Response,
    actor: Annotated[RequestActor, Depends(authenticate_request)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    if_match: Annotated[str, Header(alias="If-Match")],
    registration: Annotated[
        CommandRegistration, Depends(command_registration("update_resource_template"))
    ],
) -> ResourceTemplateSchema:
    command = registration.decode_command(
        {"template_id": template_id}, {"If-Match": if_match}, body
    )
    result = CommandService(request.app.state.session_factory).execute(
        command, _context(request, actor, idempotency_key)
    )
    return _set_etag(response, result)


@router.post(
    "/resources/{source_resource_id}/copies",
    response_model=None,
    status_code=201,
)
def copy_resource(
    source_resource_id: UUID,
    body: CopyResourceRequest,
    request: Request,
    response: Response,
    actor: Annotated[RequestActor, Depends(authenticate_request)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    registration: Annotated[
        CommandRegistration, Depends(command_registration("copy_resource"))
    ],
) -> ResourceSchema:
    command = registration.decode_command(
        {"source_resource_id": source_resource_id}, {}, body
    )
    result = CommandService(request.app.state.session_factory).execute(
        command,
        _context(request, actor, idempotency_key),
    )
    return _set_etag(response, result)


@router.post("/resources/{namespace_path:path}", response_model=None, status_code=201)
def create_resource(
    namespace_path: str,
    body: CreateResourceRequest,
    request: Request,
    response: Response,
    actor: Annotated[RequestActor, Depends(authenticate_request)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    registration: Annotated[
        CommandRegistration, Depends(command_registration("create_resource"))
    ],
) -> ResourceSchema:
    command = registration.decode_command(
        {"namespace_path": namespace_path}, {}, body
    )
    result = CommandService(request.app.state.session_factory).execute(
        command,
        _context(request, actor, idempotency_key),
    )
    return _set_etag(response, result)


@router.patch("/resources/{resource_id}", response_model=None)
def update_resource(
    resource_id: UUID,
    body: UpdateResourceRequest,
    request: Request,
    response: Response,
    actor: Annotated[RequestActor, Depends(authenticate_request)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    if_match: Annotated[str, Header(alias="If-Match")],
    registration: Annotated[
        CommandRegistration, Depends(command_registration("update_resource"))
    ],
) -> ResourceSchema:
    command = registration.decode_command(
        path_params={"resource_id": resource_id},
        headers={"If-Match": if_match},
        body=body,
    )
    result = CommandService(request.app.state.session_factory).execute(
        command,
        _context(request, actor, idempotency_key),
    )
    return _set_etag(response, result)


@router.post(
    "/process-runs/{namespace_path:path}", response_model=None, status_code=201
)
def create_process_run(
    namespace_path: str,
    body: ProcessRunDraft,
    request: Request,
    response: Response,
    actor: Annotated[RequestActor, Depends(authenticate_request)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    registration: Annotated[
        CommandRegistration, Depends(command_registration("create_process_run"))
    ],
) -> ProcessRunSchema:
    command = registration.decode_command(
        {"namespace_path": namespace_path}, {}, body
    )
    result = CommandService(request.app.state.session_factory).execute(
        command, _context(request, actor, idempotency_key)
    )
    return _set_etag(response, result)


@router.post(
    "/process-runs/{source_process_run_id}/copies", response_model=None, status_code=201
)
def copy_process_run(
    source_process_run_id: UUID,
    body: CopyProcessRunRequest,
    request: Request,
    response: Response,
    actor: Annotated[RequestActor, Depends(authenticate_request)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    registration: Annotated[
        CommandRegistration, Depends(command_registration("copy_process_run"))
    ],
) -> ProcessRunSchema:
    command = registration.decode_command(
        {"source_process_run_id": source_process_run_id}, {}, body
    )
    result = CommandService(request.app.state.session_factory).execute(
        command, _context(request, actor, idempotency_key)
    )
    return _set_etag(response, result)


@router.patch("/process-runs/{process_run_id}", response_model=None)
def update_process_run(
    process_run_id: UUID,
    body: UpdateProcessRunRequest,
    request: Request,
    response: Response,
    actor: Annotated[RequestActor, Depends(authenticate_request)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    if_match: Annotated[str, Header(alias="If-Match")],
    registration: Annotated[
        CommandRegistration, Depends(command_registration("update_process_run"))
    ],
) -> ProcessRunSchema:
    command = registration.decode_command(
        {"process_run_id": process_run_id}, {"If-Match": if_match}, body
    )
    result = CommandService(request.app.state.session_factory).execute(
        command, _context(request, actor, idempotency_key)
    )
    return _set_etag(response, result)


@router.post("/lifecycle/{object_type}/{object_id}", response_model=None)
def set_lifecycle_status(
    object_type: str,
    object_id: UUID,
    body: SetLifecycleStatusRequest,
    request: Request,
    response: Response,
    actor: Annotated[RequestActor, Depends(authenticate_request)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    if_match: Annotated[str, Header(alias="If-Match")],
    registration: Annotated[
        CommandRegistration, Depends(command_registration("set_lifecycle_status"))
    ],
):
    command = registration.decode_command(
        {"object_type": object_type, "object_id": object_id},
        {"If-Match": if_match},
        body,
    )
    result = CommandService(request.app.state.session_factory).execute(
        command,
        _context(request, actor, idempotency_key),
    )
    return _set_etag(response, result)
