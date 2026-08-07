from __future__ import annotations

import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response

from recap.authentication.models import RequestActor
from recap.authorization.policy import (
    SnapshotNamespacePolicy,
    UnrestrictedNamespacePolicy,
)
from recap.commands.models import CommandContext
from recap.commands.service import CommandService
from recap.dsl.drafts import (
    ProcessRunDraft,
    ProcessTemplateDraft,
    ResourceTemplateDraft,
)
from recap.schemas.namespace import NamespaceSchema
from recap.schemas.process import ProcessRunSchema, ProcessTemplateSchema
from recap.schemas.resource import (
    ResourceCopyOptions,
    ResourceSchema,
    ResourceTemplateSchema,
)
from recap.server.audit import AuditRecord
from recap.server.errors import request_id_from
from recap.server.rest_models import (
    CopyResourceRequest,
    CreateNamespaceRequest,
    CreateResourceRequest,
    UpdateNamespaceRequest,
    UpdateResourceRequest,
)
from recap.server.security import authenticate_request

router = APIRouter(prefix="/api/v1")
_ETAG = re.compile(r'(?:W/)?"?([1-9][0-9]*)"?')


class _DiscardAuditSink:
    def emit(self, record: AuditRecord) -> None:
        pass


def _context(request: Request, actor: RequestActor, idempotency_key: str):
    provider = getattr(request.app.state, "authorization_snapshot_provider", None)
    policy = (
        SnapshotNamespacePolicy(provider.acquire())
        if provider is not None
        else getattr(
            request.app.state, "namespace_policy", UnrestrictedNamespacePolicy()
        )
    )
    return CommandContext(
        actor=actor,
        request_id=request_id_from(request),
        policy=policy,
        audit_sink=_DiscardAuditSink(),
        authorization_generation=None,
        idempotency_key=idempotency_key,
    )


def _set_etag(response: Response, entity):
    response.headers["ETag"] = f'"{entity.revision}"'
    return entity


def _parse_if_match(value: str) -> int:
    match = _ETAG.fullmatch(value.strip())
    if match is None:
        from recap.commands.errors import CommandValidationError

        raise CommandValidationError("Invalid If-Match header")
    return int(match.group(1))


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
) -> NamespaceSchema:
    result = CommandService(request.app.state.session_factory).create_namespace(
        _context(request, actor, idempotency_key),
        path=namespace_path,
        metadata=body.metadata,
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
) -> NamespaceSchema:
    result = CommandService(request.app.state.session_factory).update_namespace(
        _context(request, actor, idempotency_key),
        namespace_id=namespace_id,
        expected_revision=_parse_if_match(if_match),
        metadata=body.metadata,
        status=body.status,
    )
    return _set_etag(response, result)


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
) -> ProcessTemplateSchema:
    result = CommandService(request.app.state.session_factory).create_process_template(
        _context(request, actor, idempotency_key),
        namespace_path=namespace_path,
        draft=body,
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
) -> ProcessTemplateSchema:
    result = CommandService(request.app.state.session_factory).update_process_template(
        _context(request, actor, idempotency_key),
        template_id=template_id,
        expected_revision=_parse_if_match(if_match),
        draft=body,
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
) -> ResourceTemplateSchema:
    result = CommandService(request.app.state.session_factory).create_resource_template(
        _context(request, actor, idempotency_key),
        namespace_path=namespace_path,
        draft=body,
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
) -> ResourceTemplateSchema:
    result = CommandService(request.app.state.session_factory).update_resource_template(
        _context(request, actor, idempotency_key),
        template_id=template_id,
        expected_revision=_parse_if_match(if_match),
        draft=body,
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
) -> ResourceSchema:
    from recap.commands.models import CopyResource

    result = CommandService(request.app.state.session_factory).execute(
        CopyResource(
            source_resource_id=source_resource_id,
            destination_namespace_path=body.destination_namespace,
            options=ResourceCopyOptions.model_validate(
                body.model_dump(exclude={"destination_namespace"})
            ),
        ),
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
) -> ResourceSchema:
    from recap.commands.models import CreateResource

    result = CommandService(request.app.state.session_factory).execute(
        CreateResource(
            namespace_path=namespace_path,
            name=body.name,
            template_id=UUID(body.template_id),
            parent_id=UUID(body.parent_id) if body.parent_id else None,
            properties=body.properties,
        ),
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
) -> ResourceSchema:
    from recap.commands.models import UpdateResource

    result = CommandService(request.app.state.session_factory).execute(
        UpdateResource(
            resource_id=resource_id,
            expected_revision=_parse_if_match(if_match),
            name=body.name,
            properties=body.properties,
        ),
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
) -> ProcessRunSchema:
    from recap.commands.models import CreateProcessRun

    result = CommandService(request.app.state.session_factory).execute(
        CreateProcessRun(namespace_path=namespace_path, draft=body),
        _context(request, actor, idempotency_key),
    )
    return _set_etag(response, result)


@router.patch("/process-runs/{process_run_id}", response_model=None)
def update_process_run(
    process_run_id: UUID,
    body: dict,
    request: Request,
    response: Response,
    actor: Annotated[RequestActor, Depends(authenticate_request)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    if_match: Annotated[str, Header(alias="If-Match")],
) -> ProcessRunSchema:
    from recap.commands.models import UpdateProcessRun

    result = CommandService(request.app.state.session_factory).execute(
        UpdateProcessRun(
            process_run_id=process_run_id,
            expected_revision=_parse_if_match(if_match),
            **body,
        ),
        _context(request, actor, idempotency_key),
    )
    return _set_etag(response, result)
