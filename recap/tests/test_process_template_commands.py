from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from recap.authentication.models import ActorKind, ProviderIdentity, RequestActor
from recap.authorization.policy import UnrestrictedNamespacePolicy
from recap.authorization.scopes import Scope
from recap.commands.errors import CommandConflictError
from recap.commands.models import (
    CommandContext,
    CreateProcessTemplate,
    UpdateProcessTemplate,
)
from recap.commands.service import CommandService
from recap.db.audit import MutationAudit
from recap.db.base import Base
from recap.db.process import ProcessRun, ProcessTemplate
from recap.dsl.drafts import (
    AttributeDraft,
    AttributeGroupDraft,
    ProcessTemplateDraft,
    ResourceSlotDraft,
    StepTemplateDraft,
)
from recap.dsl.process_builder import ProcessTemplateBuilder
from recap.lifecycle import LifecycleStatus
from recap.utils.general import Direction


class AuditCollector:
    def __init__(self):
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def command_setup(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'process-commands.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    actor = RequestActor(
        actor_id="actor-1",
        kind=ActorKind.USER,
        identities=(ProviderIdentity(provider="test", subject="actor-1"),),
        credential_scopes=frozenset(Scope),
        namespace_restrictions=None,
        credential_fingerprint="fingerprint",
    )
    audit = AuditCollector()
    context = CommandContext(
        actor=actor,
        request_id=str(uuid4()),
        policy=UnrestrictedNamespacePolicy(),
        audit_sink=audit,
        authorization_generation=None,
        idempotency_key="namespace-1",
    )
    service = CommandService(factory)
    namespace = service.create_namespace(context, path="beamline/amx", metadata={})
    return (
        service,
        factory,
        replace(context, idempotency_key="template-1"),
        namespace,
        audit,
    )


def process_draft(*, labels=None):
    return ProcessTemplateDraft(
        name="screening",
        version="1.0",
        labels=labels or ["MX"],
        resource_slots=[
            ResourceSlotDraft(
                name="sample",
                resource_type="container",
                direction=Direction.input,
                create_resource_type=True,
            )
        ],
        steps=[
            StepTemplateDraft(
                name="collect",
                role_bindings={"source": "sample"},
                parameter_groups=[
                    AttributeGroupDraft(
                        name="exposure",
                        attributes=[
                            AttributeDraft(
                                name="duration",
                                type="float",
                                unit="s",
                                default=0.1,
                                metadata={"minimum": 0},
                            )
                        ],
                    )
                ],
            )
        ],
    )


def test_draft_rejects_duplicate_nested_names_and_unknown_role_slot():
    with pytest.raises(ValidationError, match="Duplicate resource slot name"):
        ProcessTemplateDraft(
            name="duplicate",
            version="1",
            resource_slots=[
                ResourceSlotDraft(
                    name="sample", resource_type="a", direction=Direction.input
                ),
                ResourceSlotDraft(
                    name="sample", resource_type="b", direction=Direction.output
                ),
            ],
        )

    with pytest.raises(ValidationError, match="unknown resource slot"):
        ProcessTemplateDraft(
            name="bad-role",
            version="1",
            steps=[StepTemplateDraft(name="step", role_bindings={"input": "missing"})],
        )


def test_create_persists_complete_graph_replays_and_audits(command_setup):
    service, factory, context, namespace, audit = command_setup

    created = service.create_process_template(
        context, namespace_path=namespace.path, draft=process_draft()
    )
    replay = service.create_process_template(
        context, namespace_path=namespace.path, draft=process_draft()
    )

    assert replay == created
    assert created.namespace_id == namespace.id
    assert created.status is LifecycleStatus.MUTABLE
    assert created.revision == 1
    assert created.labels == ["mx"]
    assert created.resource_slots[0].required is True
    step = created.step_templates["collect"]
    assert step.resource_slots["source"].name == "sample"
    attribute = step.attribute_group_templates[0].attribute_templates[0]
    assert (attribute.name, attribute.value_type, attribute.unit) == (
        "duration",
        "float",
        "s",
    )
    assert len(audit.records) == 2  # namespace create plus one non-replayed mutation
    with factory() as session:
        assert len(session.scalars(select(ProcessTemplate)).all()) == 1
        assert (
            session.scalars(select(MutationAudit)).all()[-1].resource_type
            == "process_template"
        )


def test_duplicate_identity_conflicts(command_setup):
    service, _, context, namespace, _ = command_setup
    service.create_process_template(
        context, namespace_path=namespace.path, draft=process_draft()
    )

    with pytest.raises(CommandConflictError, match="already exists"):
        service.create_process_template(
            replace(context, idempotency_key="template-2"),
            namespace_path=namespace.path,
            draft=process_draft(),
        )


def test_update_replaces_mutable_graph_and_increments_revision(command_setup):
    service, _, context, namespace, _ = command_setup
    created = service.create_process_template(
        context, namespace_path=namespace.path, draft=process_draft()
    )
    updated_draft = process_draft(labels=["Updated Label"])

    updated = service.update_process_template(
        replace(context, idempotency_key="template-2"),
        template_id=created.id,
        expected_revision=1,
        draft=updated_draft,
    )

    assert updated.revision == 2
    assert updated.labels == ["updated_label"]
    assert updated.step_templates["collect"].resource_slots["source"].name == "sample"


def test_first_use_freezes_template_and_rejects_update(command_setup):
    service, factory, context, namespace, audit = command_setup
    created = service.create_process_template(
        context, namespace_path=namespace.path, draft=process_draft()
    )
    with factory.begin() as session:
        template = session.get(ProcessTemplate, created.id)
        session.add(
            ProcessRun(
                namespace_id=namespace.id,
                name="run-1",
                description="freeze template",
                template=template,
            )
        )

    with pytest.raises(CommandConflictError, match="active"):
        service.update_process_template(
            replace(context, idempotency_key="template-2"),
            template_id=created.id,
            expected_revision=1,
            draft=process_draft(labels=["too-late"]),
        )

    with factory() as session:
        stored = session.get(ProcessTemplate, created.id)
        assert stored.status is LifecycleStatus.ACTIVE
        assert stored.revision == 1
        assert stored.labels == ["mx"]
    assert audit.records[-1].outcome.value == "error"


class RecordingCommandBackend:
    def __init__(self, existing=None):
        self.commands = []
        self.existing = existing

    def get_process_template(self, *args, **kwargs):
        return self.existing

    def execute(self, command, context):
        self.commands.append((command, context))
        return None


def test_builder_submits_one_complete_command_on_success(command_setup):
    _, _, context, namespace, _ = command_setup
    backend = RecordingCommandBackend()

    with ProcessTemplateBuilder(
        backend=backend,
        namespace_id=namespace.id,
        namespace_path=namespace.path,
        name="builder-template",
        version="1.0",
        command_context=context,
    ) as builder:
        builder.add_resource_slot(
            "sample",
            "container",
            Direction.input,
            create_resource_type=True,
        ).add_step("collect").bind_slot("source", "sample").param_group(
            "exposure"
        ).add_attribute("duration", "float", "s", 0.1).close_group().close_step()

    assert len(backend.commands) == 1
    command, submitted_context = backend.commands[0]
    assert isinstance(command, CreateProcessTemplate)
    assert submitted_context is context
    assert command.draft.steps[0].role_bindings == {"source": "sample"}
    assert command.draft.steps[0].parameter_groups[0].attributes[0].name == "duration"


def test_builder_submits_nothing_when_context_body_raises(command_setup):
    _, _, context, namespace, _ = command_setup
    backend = RecordingCommandBackend()

    with (
        pytest.raises(RuntimeError, match="stop"),
        ProcessTemplateBuilder(
            backend=backend,
            namespace_id=namespace.id,
            namespace_path=namespace.path,
            name="builder-template",
            version="1.0",
            command_context=context,
        ) as builder,
    ):
        builder.add_step("never-saved")
        raise RuntimeError("stop")

    assert backend.commands == []


def test_builder_serializes_existing_template_into_one_update(command_setup):
    service, _, context, namespace, _ = command_setup
    existing = service.create_process_template(
        context, namespace_path=namespace.path, draft=process_draft()
    )
    update_context = replace(context, idempotency_key="template-2")
    backend = RecordingCommandBackend(existing)

    with ProcessTemplateBuilder(
        backend=backend,
        namespace_id=namespace.id,
        namespace_path=namespace.path,
        name=None,
        version=None,
        process_template_id=existing.id,
        command_context=update_context,
    ):
        pass

    assert len(backend.commands) == 1
    command, _ = backend.commands[0]
    assert isinstance(command, UpdateProcessTemplate)
    assert command.expected_revision == 1
    assert command.draft.labels == ("mx",)
    assert command.draft.steps[0].parameter_groups[0].attributes[0].name == "duration"
