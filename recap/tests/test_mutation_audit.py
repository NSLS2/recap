import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker

from recap.authorization.policy import UnrestrictedNamespacePolicy
from recap.commands.audit import DurableAuditSink, record_failure_after_rollback
from recap.commands.context import build_local_command_context
from recap.commands.idempotency import command_fingerprint
from recap.commands.models import CreateResource
from recap.commands.service import CommandService
from recap.db.audit import MutationAudit, MutationAuditRepository
from recap.db.base import Base
from recap.db.idempotency import IdempotencyRecord, IdempotencyRepository
from recap.db.namespace import Namespace
from recap.server.audit import AuditOutcome, AuditRecord, AuditSink
from recap.server.errors import ErrorCode
from recap.utils.migrations import apply_migrations


def audit_record(*, outcome: AuditOutcome = AuditOutcome.SUCCESS) -> AuditRecord:
    return AuditRecord(
        request_id=uuid4(),
        actor_id="actor-1",
        mutation="patch_resource",
        resource_type="resource",
        resource_id="resource-1",
        outcome=outcome,
        reason_code=ErrorCode.INTERNAL_ERROR if outcome is AuditOutcome.ERROR else None,
    )


class AuditCollector:
    def __init__(self):
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_local_command_context_has_shared_actor_and_configurable_audit_sink():
    sink = AuditCollector()

    context = build_local_command_context(
        audit_sink=sink,
        request_id="request-1",
        idempotency_key="command-1",
    )

    assert context.actor.actor_id == "single-user"
    assert context.actor.identities[0].provider == "single-user"
    assert context.actor.credential_scopes
    assert context.actor.namespace_restrictions is None
    assert isinstance(context.policy, UnrestrictedNamespacePolicy)
    assert context.request_id == "request-1"
    assert context.idempotency_key == "command-1"
    assert context.audit_sink is sink


def test_local_command_context_generates_uuid_request_id():
    context = build_local_command_context()

    assert UUID(context.request_id).version == 4


def test_local_context_external_audit_does_not_duplicate_durable_record():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    sink = AuditCollector()
    context = build_local_command_context(
        audit_sink=sink,
        request_id=str(uuid4()),
        idempotency_key="create-namespace-1",
    )

    with session_factory.begin() as session:
        session.add(Namespace(path="", metadata_json={}))

    CommandService(session_factory).create_namespace(
        context,
        path="beamline",
        metadata={},
    )

    with session_factory() as session:
        assert len(sink.records) == 1
        assert len(session.scalars(select(MutationAudit)).all()) == 1


def test_durable_sink_reuses_plan_2_record_and_persists_only_sanitized_fields():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    record = audit_record()

    with session_factory.begin() as session:
        sink = DurableAuditSink(MutationAuditRepository(session))
        assert isinstance(sink, AuditSink)
        sink.emit(record)

    with session_factory() as session:
        stored = session.scalar(select(MutationAudit))

    serialized = json.dumps(stored.as_record().model_dump(mode="json"))
    assert stored.as_record() == record
    assert "secret" not in serialized
    assert "property_value" not in serialized
    assert "parameter_value" not in serialized
    assert set(MutationAudit.__table__.columns.keys()) == {
        "id",
        "request_id",
        "actor_id",
        "mutation",
        "resource_type",
        "resource_id",
        "outcome",
        "reason_code",
        "create_date",
    }


def test_success_audit_rolls_back_with_owning_mutation():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)

    with (
        pytest.raises(RuntimeError, match="domain failure"),
        session_factory.begin() as session,
    ):
        DurableAuditSink(MutationAuditRepository(session)).emit(audit_record())
        raise RuntimeError("domain failure")

    with session_factory() as session:
        assert session.scalar(select(MutationAudit)) is None


def test_failure_audit_uses_short_transaction_after_mutation_rollback():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    failure = audit_record(outcome=AuditOutcome.ERROR)

    with (
        pytest.raises(RuntimeError, match="domain failure"),
        session_factory.begin(),
    ):
        raise RuntimeError("domain failure")

    record_failure_after_rollback(session_factory, failure)

    with session_factory() as session:
        assert session.scalar(select(MutationAudit)).as_record() == failure


def test_mutation_success_audit_and_idempotency_result_are_one_transaction():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    source_id = uuid4()
    fingerprint = command_fingerprint(
        method="PATCH",
        route_template="/namespaces/{namespace}",
        namespace_path="beamline/endstation",
        source_id=source_id,
        body=CreateResource(
            namespace_path="beamline/endstation",
            name="updated",
            template_id=uuid4(),
        ),
    )

    with (
        pytest.raises(RuntimeError, match="force rollback"),
        session_factory.begin() as session,
    ):
        session.add(Namespace(id=source_id, path="beamline/endstation"))
        repository = IdempotencyRepository(session)
        decision = repository.claim("actor-1", "command-1", fingerprint, lambda _: None)
        DurableAuditSink(MutationAuditRepository(session)).emit(audit_record())
        repository.complete(
            decision, target_id=str(source_id), response={"revision": 1}
        )
        raise RuntimeError("force rollback")

    with session_factory() as session:
        assert session.get(Namespace, source_id) is None
        assert session.scalar(select(MutationAudit)) is None
        assert session.scalar(select(IdempotencyRecord)) is None


def test_migration_head_creates_command_infrastructure(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    apply_migrations(str(engine.url))

    tables = set(inspect(engine).get_table_names())

    assert {"command_idempotency", "mutation_audit"} <= tables
