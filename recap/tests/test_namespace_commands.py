from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from recap.adapter.local import LocalBackend
from recap.authentication.models import ActorKind, ProviderIdentity, RequestActor
from recap.authorization.policy import UnrestrictedNamespacePolicy
from recap.authorization.scopes import Scope
from recap.client.permissions import EffectivePermissions
from recap.commands.errors import CommandConflictError, CommandValidationError
from recap.commands.models import CommandContext
from recap.commands.service import CommandService
from recap.db.audit import MutationAudit
from recap.db.base import Base
from recap.db.namespace import Namespace
from recap.lifecycle import LifecycleStatus
from recap.server.audit import AuditOutcome


class AuditCollector:
    def __init__(self):
        self.records = []

    def emit(self, record):
        self.records.append(record)


class RecordingPolicy(UnrestrictedNamespacePolicy):
    def __init__(self):
        self.paths = []

    def permissions_for(self, actor, namespace_path):
        self.paths.append(namespace_path)
        return super().permissions_for(actor, namespace_path)


class DenyingPolicy:
    def permissions_for(self, actor, namespace_path):
        return EffectivePermissions(
            identities=actor.identities,
            snapshot_generation="generation-1",
            effective_scopes=frozenset(),
            matched_namespace_paths=(),
            grants=(),
        )


@pytest.fixture
def command_setup(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'commands.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(Namespace(path="", metadata_json={}))
    actor = RequestActor(
        actor_id="actor-1",
        kind=ActorKind.USER,
        identities=(ProviderIdentity(provider="test", subject="actor-1"),),
        credential_scopes=frozenset(Scope),
        namespace_restrictions=None,
        credential_fingerprint="fingerprint",
    )
    audit = AuditCollector()
    policy = RecordingPolicy()
    context = CommandContext(
        actor=actor,
        request_id=str(uuid4()),
        policy=policy,
        audit_sink=audit,
        authorization_generation=None,
        idempotency_key="namespace-command-1",
    )
    return CommandService(factory), factory, context, policy, audit


def test_create_requires_existing_parent_and_sets_parent_id(command_setup):
    service, factory, context, policy, _ = command_setup

    parent = service.create_namespace(context, path="beamline", metadata={})
    child_context = replace(context, idempotency_key="create-child")
    created = service.create_namespace(
        child_context, path="beamline/amx", metadata={"beamline": "amx"}
    )

    assert created.path == "beamline/amx"
    assert policy.paths == ["", "beamline"]
    with factory() as session:
        stored_parent = session.get(Namespace, parent.id)
        stored_child = session.get(Namespace, created.id)
        assert stored_child.parent_id == stored_parent.id


def test_local_backend_namespace_commands_delegate(command_setup):
    _, factory, context, _, _ = command_setup
    backend = LocalBackend(factory)

    created = backend.create_namespace("beamline", {}, context)
    updated = backend.update_namespace(
        created.id,
        expected_revision=1,
        metadata={"owner": "amx"},
        status=LifecycleStatus.ACTIVE,
        context=replace(context, idempotency_key="update-namespace"),
    )

    assert created.path == "beamline"
    assert updated.metadata == {"owner": "amx"}
    assert updated.revision == 2


def test_create_missing_parent_fails_with_actionable_validation_error(command_setup):
    service, factory, context, policy, audit = command_setup

    with pytest.raises(
        CommandValidationError,
        match=r"Parent namespace 'beamline' does not exist.*beamline/amx",
    ):
        service.create_namespace(context, path="beamline/amx", metadata={})

    assert policy.paths == ["beamline"]
    assert audit.records[-1].outcome is AuditOutcome.ERROR
    with factory() as session:
        assert (
            session.scalar(select(Namespace).where(Namespace.path == "beamline/amx"))
            is None
        )


def test_missing_parent_does_not_consume_idempotency_key(command_setup):
    service, _, context, _, _ = command_setup

    with pytest.raises(CommandValidationError):
        service.create_namespace(context, path="beamline/amx", metadata={})

    parent_context = replace(context, idempotency_key="create-parent")
    child_context = replace(context, idempotency_key="create-child")
    service.create_namespace(parent_context, path="beamline", metadata={})
    created = service.create_namespace(child_context, path="beamline/amx", metadata={})

    assert created.path == "beamline/amx"


def test_top_level_create_authorizes_root(command_setup):
    service, _, context, policy, _ = command_setup

    service.create_namespace(context, path="beamline", metadata={})

    assert policy.paths == [""]


def test_create_replay_returns_exact_result_without_duplicate_mutation(command_setup):
    service, factory, context, policy, audit = command_setup
    service.create_namespace(
        replace(context, idempotency_key="create-parent"),
        path="beamline",
        metadata={},
    )
    first = service.create_namespace(context, path="beamline/amx", metadata={"a": 1})

    replay = service.create_namespace(context, path="beamline/amx", metadata={"a": 1})

    assert replay == first
    assert policy.paths == ["", "beamline", "beamline"]
    assert len(audit.records) == 2
    with factory() as session:
        assert (
            len(
                session.scalars(
                    select(Namespace).where(Namespace.path != "")
                ).all()
            )
            == 2
        )


def test_idempotency_key_reuse_for_different_create_conflicts(command_setup):
    service, _, context, _, _ = command_setup
    service.create_namespace(
        replace(context, idempotency_key="create-parent"),
        path="beamline",
        metadata={},
    )
    service.create_namespace(context, path="beamline/amx", metadata={})

    with pytest.raises(CommandConflictError, match="different command"):
        service.create_namespace(context, path="beamline/fmx", metadata={})


def test_update_checks_revision_merges_metadata_and_transitions_status(command_setup):
    service, _, context, _, _ = command_setup
    service.create_namespace(
        replace(context, idempotency_key="create-parent"),
        path="beamline",
        metadata={},
    )
    created = service.create_namespace(
        context, path="beamline/amx", metadata={"a": 1, "owner": "old"}
    )
    update_context = replace(context, idempotency_key="namespace-command-2")

    updated = service.update_namespace(
        update_context,
        namespace_id=created.id,
        metadata={"owner": "amx"},
        status=LifecycleStatus.ACTIVE,
        expected_revision=1,
    )

    assert updated.metadata == {"a": 1, "owner": "amx"}
    assert updated.status is LifecycleStatus.ACTIVE
    assert updated.revision == 2


def test_stale_update_rolls_back_mutation_and_records_failure(command_setup):
    service, factory, context, _, audit = command_setup
    service.create_namespace(
        replace(context, idempotency_key="create-parent"),
        path="beamline",
        metadata={},
    )
    created = service.create_namespace(context, path="beamline/amx", metadata={})

    with pytest.raises(CommandConflictError, match="revision"):
        service.update_namespace(
            replace(context, idempotency_key="namespace-command-2"),
            namespace_id=created.id,
            metadata={"wrong": True},
            expected_revision=2,
        )

    with factory() as session:
        stored = session.get(Namespace, created.id)
        assert stored.metadata_json == {}
        assert stored.revision == 1
        assert session.scalars(select(MutationAudit)).all()[-1].outcome == "error"
    assert audit.records[-1].outcome is AuditOutcome.ERROR


def test_invalid_status_transition_rolls_back(command_setup):
    service, _, context, _, _ = command_setup
    service.create_namespace(
        replace(context, idempotency_key="create-parent"),
        path="beamline",
        metadata={},
    )
    created = service.create_namespace(context, path="beamline/amx", metadata={})
    active = service.update_namespace(
        replace(context, idempotency_key="namespace-command-2"),
        namespace_id=created.id,
        status=LifecycleStatus.ACTIVE,
        expected_revision=1,
    )

    with pytest.raises(CommandValidationError, match="lifecycle transition"):
        service.update_namespace(
            replace(context, idempotency_key="namespace-command-3"),
            namespace_id=active.id,
            status=LifecycleStatus.MUTABLE,
            expected_revision=2,
        )


def test_denied_create_does_not_open_mutation(command_setup):
    service, factory, context, _, audit = command_setup

    with pytest.raises(Exception, match="Authorization denied"):
        service.create_namespace(
            replace(context, policy=DenyingPolicy()), path="beamline/amx", metadata={}
        )

    with factory() as session:
        assert (
            session.scalar(select(Namespace).where(Namespace.path == "beamline/amx"))
            is None
        )
    assert audit.records[-1].outcome is AuditOutcome.DENIED
