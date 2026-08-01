from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

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
    CopyResource,
    CreateProcessRun,
    CreateProcessTemplate,
    CreateResource,
    CreateResourceTemplate,
    UpdateProcessRun,
    UpdateProcessTemplate,
    UpdateResource,
    UpdateResourceTemplate,
)
from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.db.audit import MutationAuditRepository
from recap.db.base import compare_and_swap_revision
from recap.db.idempotency import IdempotencyRepository
from recap.db.namespace import Namespace, NamespaceRepository
from recap.db.process import (
    ProcessRun,
    ProcessTemplate,
    ResourceSlot,
)
from recap.db.resource import Property, Resource, ResourceTemplate, ResourceType
from recap.db.step import StepTemplate, StepTemplateResourceSlotBinding
from recap.dsl.drafts import (
    ProcessRunDraft,
    ProcessTemplateDraft,
    ResourceTemplateDraft,
)
from recap.lifecycle import LifecycleStatus, validate_transition
from recap.schemas.attribute import AttributeTemplateValidator
from recap.schemas.namespace import NamespaceSchema
from recap.schemas.process import ProcessRunSchema, ProcessTemplateSchema
from recap.schemas.resource import ResourceCopyOptions, ResourceSchema
from recap.schemas.step import ParameterSchema, StepSchema
from recap.server.audit import AuditOutcome, AuditRecord
from recap.server.errors import AuthorizationDenied, ErrorCode
from recap.utils.namespace import (
    canonicalize_namespace_path,
    is_namespace_ancestor,
    parent_namespace_path,
)


class _CreateFingerprint(BaseModel):
    metadata: dict[str, Any]


class _CreateResourceFingerprint(BaseModel):
    name: str
    template_id: UUID
    parent_id: UUID | None
    properties: dict[str, dict[str, object]] | None


class _UpdateResourceFingerprint(BaseModel):
    name: str | None
    properties: dict[str, dict[str, object]] | None
    expected_revision: int


class _UpdateFingerprint(BaseModel):
    metadata: dict[str, Any] | None
    status: LifecycleStatus | None
    expected_revision: int


class _UpdateProcessTemplateFingerprint(BaseModel):
    draft: ProcessTemplateDraft
    expected_revision: int


class _UpdateResourceTemplateFingerprint(BaseModel):
    draft: ResourceTemplateDraft
    expected_revision: int


class _UpdateProcessRunFingerprint(BaseModel):
    description: str | None
    status: str | None
    assignments: dict[str, UUID] | None
    steps: dict[str, dict[str, dict[str, object]]] | None
    expected_revision: int


class CommandService:
    """Execute namespace writes with one owning database transaction."""

    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def execute(self, command: CommandModel, context: CommandContext):
        if isinstance(command, CreateResource):
            return self.create_resource(
                context,
                namespace_path=command.namespace_path,
                name=command.name,
                template_id=command.template_id,
                parent_id=command.parent_id,
                properties=command.properties,
            )
        if isinstance(command, UpdateResource):
            return self.update_resource(
                context,
                resource_id=command.resource_id,
                expected_revision=command.expected_revision,
                name=command.name,
                properties=command.properties,
            )
        if isinstance(command, CopyResource):
            return self.copy_resource(
                context,
                source_resource_id=command.source_resource_id,
                destination_namespace_path=command.destination_namespace_path,
                options=command.options,
            )
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
        if isinstance(command, CreateResourceTemplate):
            return self.create_resource_template(
                context, namespace_path=command.namespace_path, draft=command.draft
            )
        if isinstance(command, UpdateResourceTemplate):
            return self.update_resource_template(
                context,
                template_id=command.template_id,
                expected_revision=command.expected_revision,
                draft=command.draft,
            )
        if isinstance(command, CreateProcessRun):
            return self.create_process_run(
                context, namespace_path=command.namespace_path, draft=command.draft
            )
        if isinstance(command, UpdateProcessRun):
            return self.update_process_run(
                context,
                process_run_id=command.process_run_id,
                expected_revision=command.expected_revision,
                description=command.description,
                status=command.status,
                assignments=command.assignments,
                steps=command.steps,
            )
        raise CommandValidationError(
            f"Unsupported command type: {type(command).__name__}"
        )

    def create_resource(  # noqa: C901
        self,
        context: CommandContext,
        *,
        namespace_path: str,
        name: str,
        template_id: UUID,
        parent_id: UUID | None = None,
        properties: dict[str, dict[str, object]] | None = None,
    ) -> ResourceSchema:
        try:
            canonical_path = canonicalize_namespace_path(namespace_path)
        except ValueError as error:
            raise CommandValidationError(str(error)) from error
        self._authorize_scope(
            context, canonical_path, Scope.RESOURCE_WRITE, "create_resource"
        )
        fingerprint = command_fingerprint(
            method="POST",
            route_template="/api/v1/resources/{namespace_path:path}",
            namespace_path=canonical_path,
            source_id=None,
            body=_CreateResourceFingerprint(
                name=name,
                template_id=template_id,
                parent_id=parent_id,
                properties=properties,
            ),
        )
        try:
            with self._session_factory.begin() as session:
                namespace = session.scalar(
                    select(Namespace).where(Namespace.path == canonical_path)
                )
                if namespace is None:
                    raise CommandNotFoundError("Namespace not found")
                template = session.get(ResourceTemplate, template_id)
                if template is None:
                    raise CommandNotFoundError("Resource template not found")
                parent = session.get(Resource, parent_id) if parent_id else None
                if parent_id and parent is None:
                    raise CommandNotFoundError("Parent resource not found")
                if parent and parent.namespace_id != namespace.id:
                    raise CommandValidationError(
                        "Parent resource belongs to another namespace"
                    )
                idempotency = IdempotencyRepository(session)
                decision = self._claim(
                    idempotency, context, fingerprint, lambda _id: None
                )
                if decision is not None and decision.replayed:
                    assert decision.response is not None
                    return ResourceSchema.model_validate(decision.response)
                resource = Resource(
                    namespace=namespace,
                    name=name,
                    template=template,
                    parent=parent,
                )
                session.add(resource)
                if properties:
                    self._apply_resource_changes(resource, properties)
                session.flush()
                result = ResourceSchema.model_validate(resource)
                response = result.model_dump(mode="json")
                if decision is not None:
                    idempotency.complete(
                        decision, target_id=str(resource.id), response=response
                    )
                self._emit_success(
                    session,
                    context,
                    "create_resource",
                    str(resource.id),
                    resource_type="resource",
                )
                return result
        except IntegrityError as error:
            mapped = CommandConflictError("Resource name already exists under parent")
            self._emit_failure(
                context, "create_resource", None, mapped, resource_type="resource"
            )
            raise mapped from error
        except Exception as error:
            self._emit_failure(
                context, "create_resource", None, error, resource_type="resource"
            )
            raise

    def update_resource(  # noqa: C901
        self,
        context: CommandContext,
        *,
        resource_id: UUID,
        expected_revision: int,
        name: str | None = None,
        properties: dict[str, dict[str, object]] | None = None,
    ) -> ResourceSchema:
        if expected_revision < 1:
            raise CommandValidationError("Expected revision must be positive")
        if name is None and not properties:
            raise CommandValidationError("Resource update is empty")
        fingerprint = command_fingerprint(
            method="PATCH",
            route_template="/api/v1/resources/{resource_id}",
            namespace_path=None,
            source_id=resource_id,
            body=_UpdateResourceFingerprint(
                name=name, properties=properties, expected_revision=expected_revision
            ),
        )
        try:
            with self._session_factory.begin() as session:
                resource = session.get(Resource, resource_id)
                if resource is None:
                    raise CommandNotFoundError("Resource not found")
                self._authorize_scope(
                    context,
                    resource.namespace.path,
                    Scope.RESOURCE_WRITE,
                    "update_resource",
                )
                idempotency = IdempotencyRepository(session)
                decision = self._claim(
                    idempotency, context, fingerprint, lambda _id: None
                )
                if decision is not None and decision.replayed:
                    assert decision.response is not None
                    return ResourceSchema.model_validate(decision.response)
                if resource.status is not LifecycleStatus.MUTABLE:
                    raise CommandConflictError("Cannot update an active resource")
                if name is not None:
                    duplicate = session.scalar(
                        select(Resource.id).where(
                            Resource.parent_id == resource.parent_id,
                            Resource.name == name,
                            Resource.id != resource.id,
                        )
                    )
                    if duplicate is not None:
                        raise CommandConflictError(
                            "Resource name already exists under parent"
                        )
                    resource.name = name
                if properties:
                    self._apply_resource_changes(resource, properties)
                compare_and_swap_revision(
                    session,
                    Resource,
                    resource_id,
                    expected_revision=expected_revision,
                    values={},
                )
                session.flush()
                session.refresh(resource)
                result = ResourceSchema.model_validate(resource)
                response = result.model_dump(mode="json")
                if decision is not None:
                    idempotency.complete(
                        decision, target_id=str(resource.id), response=response
                    )
                self._emit_success(
                    session,
                    context,
                    "update_resource",
                    str(resource.id),
                    resource_type="resource",
                )
                return result
        except Exception as error:
            self._emit_failure(
                context,
                "update_resource",
                str(resource_id),
                error,
                resource_type="resource",
            )
            raise

    def copy_resource(  # noqa: C901
        self,
        context: CommandContext,
        *,
        source_resource_id: UUID,
        destination_namespace_path: str,
        options: ResourceCopyOptions | None = None,
    ) -> ResourceSchema:
        options = options or ResourceCopyOptions()
        try:
            destination_path = canonicalize_namespace_path(destination_namespace_path)
        except ValueError as error:
            raise CommandValidationError(str(error)) from error
        fingerprint = command_fingerprint(
            method="POST",
            route_template="/api/v1/resources/{source_resource_id}/copies/{destination_namespace_path:path}",
            namespace_path=destination_path,
            source_id=source_resource_id,
            body=options,
        )
        try:
            with self._session_factory.begin() as session:
                source = session.get(Resource, source_resource_id)
                if source is None:
                    raise CommandNotFoundError("Resource not found")
                destination = session.scalar(
                    select(Namespace).where(Namespace.path == destination_path)
                )
                if destination is None:
                    raise CommandNotFoundError("Destination namespace not found")
                self._authorize_scope(
                    context, source.namespace.path, Scope.RESOURCE_READ, "copy_resource"
                )
                self._authorize_scope(
                    context,
                    destination.path,
                    Scope.RESOURCE_WRITE,
                    "copy_resource",
                    audit_denial=False,
                )
                if source.parent_id is not None:
                    raise CommandValidationError(
                        "Source resource must be a resource graph root"
                    )
                if not is_namespace_ancestor(source.namespace.path, destination.path):
                    raise CommandValidationError(
                        "Destination namespace must be the source namespace or its descendant"
                    )
                idempotency = IdempotencyRepository(session)
                decision = self._claim(
                    idempotency, context, fingerprint, lambda _id: None
                )
                if decision is not None and decision.replayed:
                    assert decision.response is not None
                    return ResourceSchema.model_validate(decision.response)
                flat = self._load_resource_tree(session, source)
                children = {}
                for item in flat:
                    if item.parent_id is not None:
                        children.setdefault(item.parent_id, []).append(item)

                def clone(original, parent=None):
                    result = Resource(
                        id=uuid4(),
                        name=original.name,
                        template=original.template,
                        namespace=destination,
                        parent=parent,
                        status=LifecycleStatus.MUTABLE,
                        revision=1,
                        _init_children=False,
                    )
                    for original_property in original.properties.values():
                        copied = Property(
                            id=uuid4(),
                            template=original_property.template,
                            resource=result,
                        )
                        for key, value in original_property._values.items():
                            target = copied._values[key]
                            target.id = uuid4()
                            target.value_json = deepcopy(value.value_json)
                            target.unit = value.unit
                            target.metadata_json = deepcopy(value.metadata_json)
                    for child in children.get(original.id, []):
                        clone(child, result)
                    return result

                copied = clone(source)
                copied.copied_from = source
                if options.name is not None:
                    copied.name = options.name
                session.add(copied)
                self._apply_resource_changes(copied, options.changes.properties)
                if source.status is LifecycleStatus.MUTABLE:
                    source.activate()
                session.flush()
                result = ResourceSchema.model_validate(copied)
                response = result.model_dump(mode="json")
                if decision is not None:
                    idempotency.complete(
                        decision, target_id=str(copied.id), response=response
                    )
                self._emit_success(
                    session,
                    context,
                    "copy_resource",
                    str(copied.id),
                    resource_type="resource",
                )
                return result
        except Exception as error:
            self._emit_failure(
                context,
                "copy_resource",
                str(source_resource_id),
                error,
                resource_type="resource",
            )
            raise

    @staticmethod
    def _load_resource_tree(session, root):
        result = [root]
        pending = [root.id]
        while pending:
            children = list(
                session.scalars(
                    select(Resource).where(Resource.parent_id.in_(pending))
                ).all()
            )
            result.extend(children)
            pending = [child.id for child in children]
        return result

    @staticmethod
    def _apply_resource_changes(resource, changes):
        for group_name, values in changes.items():
            prop = next(
                (
                    item
                    for item in resource.properties.values()
                    if group_name in {item.template.name, item.template.slug}
                ),
                None,
            )
            if prop is None:
                raise CommandValidationError(
                    f"Copied resource has no property group {group_name!r}"
                )
            for attribute_name, raw_value in values.items():
                value = next(
                    (
                        item
                        for item in prop._values.values()
                        if attribute_name in {item.template.name, item.template.slug}
                    ),
                    None,
                )
                if value is None:
                    raise CommandValidationError(
                        f"Property {attribute_name!r} not found in group {group_name!r}"
                    )
                if isinstance(raw_value, dict):
                    value.set_value(deepcopy(raw_value.get("value")))
                    if "unit" in raw_value:
                        value.unit = raw_value["unit"]
                    if "metadata_json" in raw_value:
                        value.metadata_json = deepcopy(raw_value["metadata_json"])
                else:
                    value.set_value(deepcopy(raw_value))

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

    def create_resource_template(
        self,
        context: CommandContext,
        *,
        namespace_path: str,
        draft: ResourceTemplateDraft,
    ):
        try:
            canonical_path = canonicalize_namespace_path(namespace_path)
        except ValueError as error:
            raise CommandValidationError(str(error)) from error
        self._authorize(context, canonical_path, mutation="create_resource_template")
        fingerprint = command_fingerprint(
            method="POST",
            route_template="/api/v1/namespaces/{namespace_path:path}/resource-templates",
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
                    idempotency, context, fingerprint, lambda _id: None
                )
                if decision is not None and decision.replayed:
                    assert decision.response is not None
                    from recap.schemas.resource import ResourceTemplateSchema

                    return ResourceTemplateSchema.model_validate(decision.response)
                template = self._materialize_resource_template(
                    session, namespace, draft
                )
                session.flush()
                from recap.schemas.resource import ResourceTemplateSchema

                result = ResourceTemplateSchema.model_validate(template)
                response = result.model_dump(mode="json")
                if decision is not None:
                    idempotency.complete(
                        decision, target_id=str(template.id), response=response
                    )
                self._emit_success(
                    session,
                    context,
                    "create_resource_template",
                    str(template.id),
                    resource_type="resource_template",
                )
                return result
        except IntegrityError as error:
            mapped = CommandConflictError(
                "Resource template name/version already exists in namespace"
            )
            self._emit_failure(
                context,
                "create_resource_template",
                None,
                mapped,
                resource_type="resource_template",
            )
            raise mapped from error
        except Exception as error:
            self._emit_failure(
                context,
                "create_resource_template",
                None,
                error,
                resource_type="resource_template",
            )
            raise

    def update_resource_template(
        self,
        context: CommandContext,
        *,
        template_id: UUID,
        expected_revision: int,
        draft: ResourceTemplateDraft,
    ):
        if expected_revision < 1:
            raise CommandValidationError("Expected revision must be positive")
        fingerprint = command_fingerprint(
            method="PATCH",
            route_template="/api/v1/resource-templates/{template_id}",
            namespace_path=None,
            source_id=template_id,
            body=_UpdateResourceTemplateFingerprint(
                draft=draft, expected_revision=expected_revision
            ),
        )
        try:
            with self._session_factory.begin() as session:
                template = session.get(ResourceTemplate, template_id)
                if template is None:
                    raise CommandNotFoundError("Resource template not found")
                self._authorize(
                    context,
                    template.namespace.path,
                    mutation="update_resource_template",
                )
                idempotency = IdempotencyRepository(session)
                decision = self._claim(
                    idempotency, context, fingerprint, lambda _id: None
                )
                if decision is not None and decision.replayed:
                    assert decision.response is not None
                    from recap.schemas.resource import ResourceTemplateSchema

                    return ResourceTemplateSchema.model_validate(decision.response)
                if template.status is not LifecycleStatus.MUTABLE:
                    raise CommandConflictError(
                        "Cannot update an active resource template"
                    )
                duplicate = session.scalar(
                    select(ResourceTemplate.id).where(
                        ResourceTemplate.namespace_id == template.namespace_id,
                        ResourceTemplate.parent_id == template.parent_id,
                        ResourceTemplate.name == draft.name,
                        ResourceTemplate.version == draft.version,
                        ResourceTemplate.id != template.id,
                    )
                )
                if duplicate is not None:
                    raise CommandConflictError(
                        "Resource template name/version already exists in namespace"
                    )
                # Build replacement graph under a savepoint before advancing
                # revision. Readers only observe changes after outer commit.
                with session.begin_nested():
                    self._clear_resource_template(session, template)
                    self._materialize_resource_contents(session, template, draft)
                    session.flush()
                compare_and_swap_revision(
                    session,
                    ResourceTemplate,
                    template_id,
                    expected_revision=expected_revision,
                    values={
                        "name": draft.name,
                        "version": draft.version,
                        "labels": list(draft.labels),
                    },
                )
                session.flush()
                session.refresh(template)
                from recap.schemas.resource import ResourceTemplateSchema

                result = ResourceTemplateSchema.model_validate(template)
                response = result.model_dump(mode="json")
                if decision is not None:
                    idempotency.complete(
                        decision, target_id=str(template.id), response=response
                    )
                self._emit_success(
                    session,
                    context,
                    "update_resource_template",
                    str(template.id),
                    resource_type="resource_template",
                )
                return result
        except Exception as error:
            self._emit_failure(
                context,
                "update_resource_template",
                str(template_id),
                error,
                resource_type="resource_template",
            )
            raise

    def create_process_run(
        self, context: CommandContext, *, namespace_path: str, draft: ProcessRunDraft
    ):
        try:
            target_path = canonicalize_namespace_path(namespace_path)
        except ValueError as error:
            raise CommandValidationError(str(error)) from error
        self._authorize_scope(
            context, target_path, Scope.PROCESS_RUN_WRITE, "create_process_run"
        )
        fingerprint = command_fingerprint(
            method="POST",
            route_template="/api/v1/process-runs/{namespace_path:path}",
            namespace_path=target_path,
            source_id=None,
            body=draft,
        )
        try:
            with self._session_factory.begin() as session:
                namespace = session.scalar(
                    select(Namespace).where(Namespace.path == target_path)
                )
                if namespace is None:
                    raise CommandNotFoundError("Namespace not found")
                template = session.get(ProcessTemplate, draft.template_id)
                if template is None:
                    raise CommandNotFoundError("Process template not found")
                self._authorize_scope(
                    context,
                    template.namespace.path,
                    Scope.PROCESS_TEMPLATE_READ,
                    "create_process_run",
                )
                decision = self._claim(
                    IdempotencyRepository(session),
                    context,
                    fingerprint,
                    lambda _id: None,
                )
                if decision is not None and decision.replayed:
                    return ProcessRunSchema.model_validate(decision.response)
                run = ProcessRun(
                    namespace=namespace,
                    name=draft.name,
                    description=draft.description,
                    template=template,
                )
                session.add(run)
                session.flush()
                self._apply_run_assignments(session, run, draft.assignments, context)
                self._apply_run_steps(run, draft.steps)
                session.flush()
                session.expire_all()
                run = session.get(ProcessRun, run.id)
                result = self._process_run_schema(run)
                if decision is not None:
                    idempotency = IdempotencyRepository(session)
                    idempotency.complete(
                        decision,
                        target_id=str(run.id),
                        response=result.model_dump(mode="json"),
                    )
                self._emit_success(
                    session,
                    context,
                    "create_process_run",
                    str(run.id),
                    resource_type="process_run",
                )
                return result
        except IntegrityError as error:
            mapped = CommandConflictError("Process run already exists in namespace")
            self._emit_failure(
                context, "create_process_run", None, mapped, resource_type="process_run"
            )
            raise mapped from error
        except Exception as error:
            self._emit_failure(
                context, "create_process_run", None, error, resource_type="process_run"
            )
            raise

    def update_process_run(
        self,
        context: CommandContext,
        *,
        process_run_id: UUID,
        expected_revision: int,  # noqa: C901
        description: str | None = None,
        status: str | None = None,
        assignments: dict[str, UUID] | None = None,
        steps: dict[str, dict[str, dict[str, object]]] | None = None,
    ):
        if expected_revision < 1:
            raise CommandValidationError("Expected revision must be positive")
        fingerprint = command_fingerprint(
            method="PATCH",
            route_template="/api/v1/process-runs/{process_run_id}",
            namespace_path=None,
            source_id=process_run_id,
            body=_UpdateProcessRunFingerprint(
                description=description,
                status=status,
                assignments=assignments,
                steps=steps,
                expected_revision=expected_revision,
            ),
        )
        try:
            with self._session_factory.begin() as session:
                run = session.get(ProcessRun, process_run_id)
                if run is None:
                    raise CommandNotFoundError("Process run not found")
                self._authorize_scope(
                    context,
                    run.namespace.path,
                    Scope.PROCESS_RUN_WRITE,
                    "update_process_run",
                )
                decision = self._claim(
                    IdempotencyRepository(session),
                    context,
                    fingerprint,
                    lambda _id: None,
                )
                if decision is not None and decision.replayed:
                    return ProcessRunSchema.model_validate(decision.response)
                if (
                    description is None
                    and status is None
                    and assignments is None
                    and steps is None
                ):
                    raise CommandValidationError("Process run update is empty")
                if run.status is not LifecycleStatus.MUTABLE and (
                    description is not None
                    or assignments is not None
                    or steps is not None
                ):
                    raise CommandConflictError("Cannot update a finalized process run")
                if assignments is not None:
                    self._apply_run_assignments(session, run, assignments, context)
                if steps is not None:
                    self._apply_run_steps(run, steps)
                values: dict[str, Any] = {}
                if description is not None:
                    values["description"] = description
                if status is not None:
                    try:
                        target_status = LifecycleStatus(status)
                        if target_status is LifecycleStatus.ACTIVE:
                            run.finalize()
                        else:
                            validate_transition(run.status, target_status)
                            values["status"] = target_status
                    except ValueError as error:
                        raise CommandValidationError(str(error)) from error
                compare_and_swap_revision(
                    session,
                    ProcessRun,
                    process_run_id,
                    expected_revision=expected_revision,
                    values=values,
                )
                session.flush()
                session.expire_all()
                run = session.get(ProcessRun, process_run_id)
                session.refresh(run)
                result = self._process_run_schema(run)
                if decision is not None:
                    IdempotencyRepository(session).complete(
                        decision,
                        target_id=str(run.id),
                        response=result.model_dump(mode="json"),
                    )
                self._emit_success(
                    session,
                    context,
                    "update_process_run",
                    str(run.id),
                    resource_type="process_run",
                )
                return result
        except Exception as error:
            self._emit_failure(
                context,
                "update_process_run",
                str(process_run_id),
                error,
                resource_type="process_run",
            )
            raise

    @staticmethod
    def _process_run_schema(run):
        result = ProcessRunSchema.model_validate(run)
        for orm_step in run.steps.values():
            current = result.steps[orm_step.name]
            if orm_step.parameters:
                parameters = {
                    name: ParameterSchema.model_validate(
                        {
                            "id": parameter.id,
                            "create_date": parameter.create_date,
                            "modified_date": parameter.modified_date,
                            "template": parameter.template,
                            "values": {
                                value_name: {"value": value.value, "unit": value.unit}
                                for value_name, value in parameter._values.items()
                            },
                        }
                    )
                    for name, parameter in orm_step.parameters.items()
                }
                payload = current.model_dump()
                payload["parameters"] = parameters
                result.steps[orm_step.name] = StepSchema.model_validate(payload)
        return result

    @staticmethod
    def _apply_run_assignments(session, run, assignments, context):
        for slot_name, resource_id in assignments.items():
            slot = next(
                (
                    item
                    for item in run.template.resource_slots
                    if item.name == slot_name
                ),
                None,
            )
            if slot is None:
                raise CommandValidationError(f"Resource slot {slot_name!r} not found")
            resource = session.get(Resource, resource_id)
            if resource is None:
                raise CommandNotFoundError("Resource not found")
            CommandService._authorize_scope(
                context,
                resource.namespace.path,
                Scope.RESOURCE_READ,
                "create_process_run",
            )
            run.resources[slot] = resource

    @staticmethod
    def _apply_run_steps(run, steps):
        for step_name, step_data in steps.items():
            step = run.steps.get(step_name)
            if step is None:
                raise CommandValidationError(f"Step {step_name!r} not found")
            for group_name, values in (step_data.parameters or {}).items():
                param = step.parameters.get(group_name)
                if param is None:
                    raise CommandValidationError(
                        f"Parameter group {group_name!r} not found"
                    )
                for attr_name, raw in values.items():
                    value = param._values.get(attr_name)
                    if value is None:
                        value = next(
                            (
                                item
                                for key, item in param._values.items()
                                if item.template.slug == attr_name
                            ),
                            None,
                        )
                    if value is None:
                        raise CommandValidationError(
                            f"Parameter {attr_name!r} not found"
                        )
                    if isinstance(raw, dict):
                        value.set_value(raw.get("value"))
                        if "unit" in raw:
                            value.unit = raw["unit"]
                    else:
                        value.set_value(raw)

    @staticmethod
    def _materialize_resource_template(session, namespace, draft):
        template = ResourceTemplate(
            namespace=namespace,
            name=draft.name,
            version=draft.version,
            labels=list(draft.labels),
        )
        session.add(template)
        CommandService._materialize_resource_contents(session, template, draft)
        return template

    @staticmethod
    def _materialize_resource_contents(session, template, draft):
        template.types = []
        for type_name in draft.type_names:
            resource_type = session.scalar(
                select(ResourceType).where(ResourceType.name == type_name)
            )
            if resource_type is None:
                resource_type = ResourceType(name=type_name)
                session.add(resource_type)
            template.types.append(resource_type)
        for group_draft in draft.property_groups:
            group = AttributeGroupTemplate(name=group_draft.name)
            template.attribute_group_templates.append(group)
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
        for child_draft in draft.children:
            child = ResourceTemplate(
                namespace=template.namespace,
                name=child_draft.name,
                version=child_draft.version,
                labels=list(child_draft.labels),
                parent=template,
            )
            session.add(child)
            template.children[child.name] = child
            CommandService._materialize_resource_contents(session, child, child_draft)

    @staticmethod
    def _clear_resource_template(session, template):
        for child in list(template.children.values()):
            CommandService._delete_resource_template_tree(session, child)
        for group in list(template.attribute_group_templates):
            for attribute in list(group.attribute_templates):
                session.delete(attribute)
            session.delete(group)
        session.flush()
        session.expire(template, ["children", "attribute_group_templates", "types"])

    @staticmethod
    def _delete_resource_template_tree(session, template):
        for child in list(template.children.values()):
            CommandService._delete_resource_template_tree(session, child)
        for group in list(template.attribute_group_templates):
            for attribute in list(group.attribute_templates):
                session.delete(attribute)
            session.delete(group)
        session.delete(template)

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
    def _authorize_scope(
        context: CommandContext,
        namespace_path: str,
        scope: Scope,
        mutation: str,
        *,
        audit_denial: bool = True,
    ) -> None:
        permissions = context.policy.permissions_for(context.actor, namespace_path)
        if scope in permissions.effective_scopes:
            return
        if audit_denial:
            context.audit_sink.emit(
                CommandService._audit_record(
                    context,
                    mutation,
                    None,
                    AuditOutcome.DENIED,
                    DenialCode.INSUFFICIENT_SCOPE,
                    resource_type="resource",
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
