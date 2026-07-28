from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from recap.authorization.scopes import Scope
from recap.client.permissions import DenialCode
from recap.commands.audit import DurableAuditSink, record_failure_after_rollback
from recap.commands.errors import (
    CommandConflictError,
    CommandError,
    CommandNotFoundError,
    CommandValidationError,
)
from recap.commands.idempotency import command_fingerprint
from recap.commands.models import (
    CommandContext,
    CommandModel,
    CreateProcessTemplate,
    UpdateProcessTemplate,
)
from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.db.audit import MutationAuditRepository
from recap.db.base import compare_and_swap_revision
from recap.db.idempotency import IdempotencyRepository
from recap.db.namespace import Namespace, NamespaceRepository
from recap.db.process import ProcessTemplate, ResourceSlot
from recap.db.resource import ResourceType
from recap.db.step import StepTemplate, StepTemplateResourceSlotBinding
from recap.dsl.drafts import ProcessTemplateDraft
from recap.lifecycle import LifecycleStatus, validate_transition
from recap.schemas.attribute import AttributeTemplateValidator
from recap.schemas.namespace import NamespaceSchema
from recap.schemas.process import ProcessTemplateSchema
from recap.server.audit import AuditOutcome, AuditRecord
from recap.server.errors import AuthorizationDenied, ErrorCode
from recap.utils.namespace import canonicalize_namespace_path, parent_namespace_path


class _CreateFingerprint(BaseModel):
    metadata: dict[str, Any]


class _UpdateFingerprint(BaseModel):
    metadata: dict[str, Any] | None
    status: LifecycleStatus | None
    expected_revision: int


class _UpdateProcessTemplateFingerprint(BaseModel):
    draft: ProcessTemplateDraft
    expected_revision: int


class CommandService:
    """Execute namespace writes with one owning database transaction."""

    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def execute(self, command: CommandModel, context: CommandContext):
        if isinstance(command, CreateProcessTemplate):
            return self.create_process_template(
                context,
                namespace_path=command.namespace_path,
                draft=command.draft,
            )
        if isinstance(command, UpdateProcessTemplate):
            return self.update_process_template(
                context,
                template_id=command.template_id,
                expected_revision=command.expected_revision,
                draft=command.draft,
            )
        raise CommandValidationError(
            f"Unsupported command type: {type(command).__name__}"
        )

    def create_namespace(
        self,
        context: CommandContext,
        *,
        path: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> NamespaceSchema:
        try:
            canonical_path = canonicalize_namespace_path(path)
        except ValueError as error:
            raise CommandValidationError(str(error)) from error
        local_metadata = dict(metadata or {})
        self._authorize(
            context,
            parent_namespace_path(canonical_path),
            mutation="create_namespace",
        )
        fingerprint = command_fingerprint(
            method="PUT",
            route_template="/api/v1/namespaces/{namespace_path:path}",
            namespace_path=canonical_path,
            source_id=None,
            body=_CreateFingerprint(metadata=local_metadata),
        )

        try:
            with self._session_factory.begin() as session:
                idempotency = IdempotencyRepository(session)
                decision = self._claim(
                    idempotency,
                    context,
                    fingerprint,
                    lambda _target_id: None,
                )
                if decision is not None and decision.replayed:
                    assert decision.response is not None
                    return NamespaceSchema.model_validate(decision.response)

                namespace = NamespaceRepository(session).create(
                    canonical_path, local_metadata
                )
                session.flush()
                result = NamespaceSchema.model_validate(namespace)
                response = result.model_dump(mode="json")
                if decision is not None:
                    idempotency.complete(
                        decision, target_id=str(namespace.id), response=response
                    )
                self._emit_success(
                    session, context, "create_namespace", str(namespace.id)
                )
                return result
        except IntegrityError as error:
            mapped = CommandConflictError("Namespace already exists")
            self._emit_failure(context, "create_namespace", None, mapped)
            raise mapped from error
        except Exception as error:
            self._emit_failure(context, "create_namespace", None, error)
            raise

    def update_namespace(
        self,
        context: CommandContext,
        *,
        namespace_id: UUID,
        expected_revision: int,
        metadata: Mapping[str, Any] | None = None,
        status: LifecycleStatus | None = None,
    ) -> NamespaceSchema:
        if expected_revision < 1:
            raise CommandValidationError("Expected revision must be positive")
        local_metadata = None if metadata is None else dict(metadata)
        fingerprint = command_fingerprint(
            method="PATCH",
            route_template="/api/v1/namespaces/{namespace_id}",
            namespace_path=None,
            source_id=namespace_id,
            body=_UpdateFingerprint(
                metadata=local_metadata,
                status=status,
                expected_revision=expected_revision,
            ),
        )

        try:
            with self._session_factory.begin() as session:
                namespace = session.get(Namespace, namespace_id)
                if namespace is None:
                    raise CommandNotFoundError("Namespace not found")
                self._authorize(context, namespace.path, mutation="update_namespace")

                idempotency = IdempotencyRepository(session)
                decision = self._claim(
                    idempotency,
                    context,
                    fingerprint,
                    lambda _target_id: None,
                )
                if decision is not None and decision.replayed:
                    assert decision.response is not None
                    return NamespaceSchema.model_validate(decision.response)

                values: dict[str, Any] = {}
                if local_metadata is not None:
                    values["metadata_json"] = local_metadata
                if status is not None:
                    try:
                        validate_transition(namespace.status, status)
                    except ValueError as error:
                        raise CommandValidationError(str(error)) from error
                    values["status"] = status
                if not values:
                    raise CommandValidationError("Namespace update is empty")

                compare_and_swap_revision(
                    session,
                    Namespace,
                    namespace_id,
                    expected_revision=expected_revision,
                    values=values,
                )
                session.flush()
                session.refresh(namespace)
                result = NamespaceSchema.model_validate(namespace)
                response = result.model_dump(mode="json")
                if decision is not None:
                    idempotency.complete(
                        decision, target_id=str(namespace.id), response=response
                    )
                self._emit_success(
                    session, context, "update_namespace", str(namespace.id)
                )
                return result
        except Exception as error:
            self._emit_failure(context, "update_namespace", str(namespace_id), error)
            raise

    def create_process_template(
        self,
        context: CommandContext,
        *,
        namespace_path: str,
        draft: ProcessTemplateDraft,
    ) -> ProcessTemplateSchema:
        try:
            canonical_path = canonicalize_namespace_path(namespace_path)
        except ValueError as error:
            raise CommandValidationError(str(error)) from error
        self._authorize(context, canonical_path, mutation="create_process_template")
        fingerprint = command_fingerprint(
            method="POST",
            route_template=(
                "/api/v1/namespaces/{namespace_path:path}/process-templates"
            ),
            namespace_path=canonical_path,
            source_id=None,
            body=draft,
        )

        try:
            with self._session_factory.begin() as session:
                namespace = session.scalar(
                    select(Namespace).where(Namespace.path == canonical_path)
                )
                if namespace is None:
                    raise CommandNotFoundError("Namespace not found")
                idempotency = IdempotencyRepository(session)
                decision = self._claim(
                    idempotency, context, fingerprint, lambda _target_id: None
                )
                if decision is not None and decision.replayed:
                    assert decision.response is not None
                    return ProcessTemplateSchema.model_validate(decision.response)

                template = ProcessTemplate(
                    namespace=namespace,
                    name=draft.name,
                    version=draft.version,
                    labels=list(draft.labels),
                )
                session.add(template)
                self._materialize_process_template(session, template, draft)
                session.flush()
                result = ProcessTemplateSchema.model_validate(template)
                response = result.model_dump(mode="json")
                if decision is not None:
                    idempotency.complete(
                        decision, target_id=str(template.id), response=response
                    )
                self._emit_success(
                    session,
                    context,
                    "create_process_template",
                    str(template.id),
                    resource_type="process_template",
                )
                return result
        except IntegrityError as error:
            mapped = CommandConflictError("Process template already exists")
            self._emit_failure(
                context,
                "create_process_template",
                None,
                mapped,
                resource_type="process_template",
            )
            raise mapped from error
        except Exception as error:
            self._emit_failure(
                context,
                "create_process_template",
                None,
                error,
                resource_type="process_template",
            )
            raise

    def update_process_template(
        self,
        context: CommandContext,
        *,
        template_id: UUID,
        expected_revision: int,
        draft: ProcessTemplateDraft,
    ) -> ProcessTemplateSchema:
        if expected_revision < 1:
            raise CommandValidationError("Expected revision must be positive")
        fingerprint = command_fingerprint(
            method="PATCH",
            route_template="/api/v1/process-templates/{template_id}",
            namespace_path=None,
            source_id=template_id,
            body=_UpdateProcessTemplateFingerprint(
                draft=draft, expected_revision=expected_revision
            ),
        )

        try:
            with self._session_factory.begin() as session:
                template = session.get(ProcessTemplate, template_id)
                if template is None:
                    raise CommandNotFoundError("Process template not found")
                self._authorize(
                    context,
                    template.namespace.path,
                    mutation="update_process_template",
                )
                idempotency = IdempotencyRepository(session)
                decision = self._claim(
                    idempotency, context, fingerprint, lambda _target_id: None
                )
                if decision is not None and decision.replayed:
                    assert decision.response is not None
                    return ProcessTemplateSchema.model_validate(decision.response)
                if template.status is not LifecycleStatus.MUTABLE:
                    raise CommandConflictError(
                        "Cannot update an active process template"
                    )

                duplicate = session.scalar(
                    select(ProcessTemplate.id).where(
                        ProcessTemplate.namespace_id == template.namespace_id,
                        ProcessTemplate.name == draft.name,
                        ProcessTemplate.version == draft.version,
                        ProcessTemplate.id != template.id,
                    )
                )
                if duplicate is not None:
                    raise CommandConflictError("Process template already exists")

                compare_and_swap_revision(
                    session,
                    ProcessTemplate,
                    template_id,
                    expected_revision=expected_revision,
                    values={
                        "name": draft.name,
                        "version": draft.version,
                        "labels": list(draft.labels),
                    },
                )
                session.expire(template)
                session.refresh(template)
                self._clear_process_template(session, template)
                self._materialize_process_template(session, template, draft)
                session.flush()
                result = ProcessTemplateSchema.model_validate(template)
                response = result.model_dump(mode="json")
                if decision is not None:
                    idempotency.complete(
                        decision, target_id=str(template.id), response=response
                    )
                self._emit_success(
                    session,
                    context,
                    "update_process_template",
                    str(template.id),
                    resource_type="process_template",
                )
                return result
        except Exception as error:
            self._emit_failure(
                context,
                "update_process_template",
                str(template_id),
                error,
                resource_type="process_template",
            )
            raise

    @staticmethod
    def _clear_process_template(session, template: ProcessTemplate) -> None:
        for step in list(template.step_templates.values()):
            for group in list(step.attribute_group_templates):
                for attribute in list(group.attribute_templates):
                    session.delete(attribute)
            session.delete(step)
        session.flush()
        for slot in list(template.resource_slots):
            session.delete(slot)
        session.flush()
        session.expire(template, ["step_templates", "resource_slots"])

    @staticmethod
    def _materialize_process_template(
        session, template: ProcessTemplate, draft: ProcessTemplateDraft
    ) -> None:
        slots = {}
        for slot_draft in draft.resource_slots:
            resource_type = session.scalar(
                select(ResourceType).where(
                    ResourceType.name == slot_draft.resource_type
                )
            )
            if resource_type is None:
                if not slot_draft.create_resource_type:
                    raise CommandValidationError(
                        f"Resource type {slot_draft.resource_type!r} not found"
                    )
                resource_type = ResourceType(name=slot_draft.resource_type)
                session.add(resource_type)
            slot = ResourceSlot(
                name=slot_draft.name,
                resource_type=resource_type,
                direction=slot_draft.direction,
                required=slot_draft.required,
            )
            template.resource_slots.append(slot)
            slots[slot_draft.name] = slot

        for step_draft in draft.steps:
            step = StepTemplate(name=step_draft.name)
            template.step_templates[step.name] = step
            for group_draft in step_draft.parameter_groups:
                group = AttributeGroupTemplate(name=group_draft.name)
                step.attribute_group_templates.append(group)
                for attribute_draft in group_draft.attributes:
                    validated = AttributeTemplateValidator.model_validate(
                        attribute_draft.model_dump()
                    )
                    default = validated.default
                    if isinstance(default, list):
                        default = json.dumps(default, default=str)
                    group.attribute_templates.append(
                        AttributeTemplate(
                            name=validated.name,
                            value_type=validated.type,
                            unit=validated.unit,
                            default_value=default,
                            metadata_json=validated.metadata,
                        )
                    )
            for role, slot_name in step_draft.role_bindings.items():
                step.bindings[role] = StepTemplateResourceSlotBinding(
                    role=role, resource_slot=slots[slot_name]
                )

    @staticmethod
    def _claim(repository, context, fingerprint, authorize_replay):
        if context.idempotency_key is None:
            return None
        return repository.claim(
            context.actor.actor_id,
            context.idempotency_key,
            fingerprint,
            authorize_replay,
        )

    @staticmethod
    def _authorize(
        context: CommandContext,
        namespace_path: str,
        *,
        mutation: str,
        audit_denial: bool = True,
    ) -> None:
        permissions = context.policy.permissions_for(context.actor, namespace_path)
        if Scope.NAMESPACE_WRITE in permissions.effective_scopes:
            return
        if audit_denial:
            context.audit_sink.emit(
                CommandService._audit_record(
                    context,
                    mutation,
                    None,
                    AuditOutcome.DENIED,
                    DenialCode.INSUFFICIENT_SCOPE,
                )
            )
        raise AuthorizationDenied()

    @staticmethod
    def _emit_success(
        session, context, mutation, resource_id, *, resource_type="namespace"
    ) -> None:
        record = CommandService._audit_record(
            context,
            mutation,
            resource_id,
            AuditOutcome.SUCCESS,
            None,
            resource_type=resource_type,
        )
        DurableAuditSink(MutationAuditRepository(session)).emit(record)
        context.audit_sink.emit(record)

    def _emit_failure(
        self,
        context,
        mutation,
        resource_id,
        error,
        *,
        resource_type="namespace",
    ) -> None:
        if isinstance(error, AuthorizationDenied):
            return
        record = self._audit_record(
            context,
            mutation,
            resource_id,
            AuditOutcome.ERROR,
            self._error_code(error),
            resource_type=resource_type,
        )
        context.audit_sink.emit(record)
        record_failure_after_rollback(self._session_factory, record)

    @staticmethod
    def _error_code(error: Exception) -> ErrorCode:
        if isinstance(error, CommandNotFoundError):
            return ErrorCode.NOT_FOUND
        if isinstance(error, CommandConflictError):
            return ErrorCode.CONFLICT
        if isinstance(error, CommandValidationError):
            return ErrorCode.VALIDATION_ERROR
        if isinstance(error, CommandError):
            return ErrorCode.REQUEST_ERROR
        return ErrorCode.INTERNAL_ERROR

    @staticmethod
    def _audit_record(
        context,
        mutation,
        resource_id,
        outcome,
        reason_code,
        *,
        resource_type="namespace",
    ):
        return AuditRecord(
            request_id=UUID(context.request_id),
            actor_id=context.actor.actor_id,
            mutation=mutation,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            reason_code=reason_code,
        )
