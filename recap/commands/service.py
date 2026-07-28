from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from pydantic import BaseModel
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
from recap.commands.models import CommandContext
from recap.db.audit import MutationAuditRepository
from recap.db.base import compare_and_swap_revision
from recap.db.idempotency import IdempotencyRepository
from recap.db.namespace import Namespace, NamespaceRepository
from recap.lifecycle import LifecycleStatus, validate_transition
from recap.schemas.namespace import NamespaceSchema
from recap.server.audit import AuditOutcome, AuditRecord
from recap.server.errors import AuthorizationDenied, ErrorCode
from recap.utils.namespace import canonicalize_namespace_path, parent_namespace_path


class _CreateFingerprint(BaseModel):
    metadata: dict[str, Any]


class _UpdateFingerprint(BaseModel):
    metadata: dict[str, Any] | None
    status: LifecycleStatus | None
    expected_revision: int


class CommandService:
    """Execute namespace writes with one owning database transaction."""

    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

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
    def _emit_success(session, context, mutation, resource_id) -> None:
        record = CommandService._audit_record(
            context, mutation, resource_id, AuditOutcome.SUCCESS, None
        )
        DurableAuditSink(MutationAuditRepository(session)).emit(record)
        context.audit_sink.emit(record)

    def _emit_failure(self, context, mutation, resource_id, error) -> None:
        if isinstance(error, AuthorizationDenied):
            return
        record = self._audit_record(
            context,
            mutation,
            resource_id,
            AuditOutcome.ERROR,
            self._error_code(error),
        )
        context.audit_sink.emit(record)
        record_failure_after_rollback(self._session_factory, record)

    @staticmethod
    def _error_code(error: Exception) -> ErrorCode:
        if isinstance(error, CommandNotFoundError):
            return ErrorCode.NOT_FOUND
        if isinstance(error, CommandValidationError):
            return ErrorCode.VALIDATION_ERROR
        if isinstance(error, CommandError):
            return ErrorCode.REQUEST_ERROR
        return ErrorCode.INTERNAL_ERROR

    @staticmethod
    def _audit_record(context, mutation, resource_id, outcome, reason_code):
        return AuditRecord(
            request_id=UUID(context.request_id),
            actor_id=context.actor.actor_id,
            mutation=mutation,
            resource_type="namespace",
            resource_id=resource_id,
            outcome=outcome,
            reason_code=reason_code,
        )
