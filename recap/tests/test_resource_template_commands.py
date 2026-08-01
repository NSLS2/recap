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
from recap.commands.errors import CommandConflictError, CommandNotFoundError
from recap.commands.models import (
    CommandContext,
    CreateResourceTemplate,
)
from recap.commands.service import CommandService
from recap.db.audit import MutationAudit
from recap.db.base import Base
from recap.db.resource import ResourceTemplate
from recap.dsl.drafts import AttributeDraft, AttributeGroupDraft, ResourceTemplateDraft


class AuditCollector:
    def __init__(self):
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def command_setup(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'resource-template-commands.db'}")
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
        idempotency_key="template-1",
    )
    service = CommandService(factory)
    namespace = service.create_namespace(context, path="beamline/amx", metadata={})
    return (
        service,
        factory,
        replace(context, idempotency_key="resource-template-1"),
        namespace,
        audit,
    )


def template_draft(name="plate", version="1.0"):
    return ResourceTemplateDraft(
        name=name,
        version=version,
        type_names=["container", "plate"],
        property_groups=[
            AttributeGroupDraft(
                name="dimensions",
                attributes=[AttributeDraft(name="rows", type="int", default=8)],
            )
        ],
        children=[
            ResourceTemplateDraft(
                name="well",
                version="1.0",
                type_names=["container", "well"],
            )
        ],
    )


def test_resource_template_draft_rejects_duplicate_nested_names():
    with pytest.raises(ValidationError, match="Duplicate child template name"):
        ResourceTemplateDraft(
            name="plate",
            version="1.0",
            children=[
                ResourceTemplateDraft(name="well", version="1"),
                ResourceTemplateDraft(name="well", version="2"),
            ],
        )


def test_create_persists_nested_graph_replays_and_audits(command_setup):
    service, factory, context, namespace, audit = command_setup
    created = service.create_resource_template(
        context, namespace_path=namespace.path, draft=template_draft()
    )
    replay = service.create_resource_template(
        context, namespace_path=namespace.path, draft=template_draft()
    )

    assert replay.model_dump(mode="json") == created.model_dump(mode="json")
    assert created.namespace_id == namespace.id
    assert created.children["well"].types[1].name == "well"
    assert (
        created.attribute_group_templates[0].attribute_templates[0].default_value == 8
    )
    assert len(audit.records) == 2
    with factory() as session:
        assert len(session.scalars(select(ResourceTemplate)).all()) == 3
        assert (
            session.scalars(select(MutationAudit)).all()[-1].resource_type
            == "resource_template"
        )


def test_duplicate_identity_and_frozen_update_conflict(command_setup):
    service, factory, context, namespace, audit = command_setup
    created = service.create_resource_template(
        context, namespace_path=namespace.path, draft=template_draft()
    )
    with pytest.raises(CommandConflictError, match="already exists"):
        service.create_resource_template(
            replace(context, idempotency_key="resource-template-2"),
            namespace_path=namespace.path,
            draft=template_draft(),
        )

    updated = service.update_resource_template(
        replace(context, idempotency_key="resource-template-3"),
        template_id=created.id,
        expected_revision=1,
        draft=template_draft(name="plate-updated"),
    )
    assert updated.revision == 2
    with factory.begin() as session:
        stored = session.get(ResourceTemplate, created.id)
        stored.activate()

    with pytest.raises(CommandConflictError, match="active"):
        service.update_resource_template(
            replace(context, idempotency_key="resource-template-4"),
            template_id=created.id,
            expected_revision=2,
            draft=template_draft(name="too-late"),
        )
    assert audit.records[-1].outcome.value == "error"


def test_failed_nested_materialization_rolls_back(command_setup):
    service, factory, context, namespace, _ = command_setup
    with pytest.raises(CommandNotFoundError):
        service.create_resource_template(
            replace(context, idempotency_key="resource-template-bad"),
            namespace_path="beamline/missing",
            draft=template_draft(),
        )
    with factory() as session:
        assert session.scalars(select(ResourceTemplate)).all() == []


def test_failed_update_preserves_graph_and_revision(command_setup, monkeypatch):
    service, factory, context, namespace, _ = command_setup
    created = service.create_resource_template(
        context, namespace_path=namespace.path, draft=template_draft()
    )

    observed_revisions = []

    def fail_materialization(session, template, draft):
        observed_revisions.append(
            session.scalar(
                select(ResourceTemplate.revision).where(
                    ResourceTemplate.id == template.id
                )
            )
        )
        raise RuntimeError("materialization failed")

    monkeypatch.setattr(service, "_materialize_resource_contents", fail_materialization)
    with pytest.raises(RuntimeError, match="materialization failed"):
        service.update_resource_template(
            replace(context, idempotency_key="resource-template-failed-update"),
            template_id=created.id,
            expected_revision=1,
            draft=template_draft(name="replacement"),
        )
    assert observed_revisions == [1]

    with factory() as session:
        stored = session.get(ResourceTemplate, created.id)
        assert stored.revision == 1
        assert stored.name == "plate"
        assert stored.children["well"].name == "well"
        assert stored.attribute_group_templates[0].name == "dimensions"


class RecordingBackend:
    def __init__(self, existing=None):
        self.commands = []
        self.existing = existing

    def get_resource_template(self, *args, **kwargs):
        return self.existing

    def execute(self, command, context):
        self.commands.append((command, context))
        return None


def test_builder_submits_one_complete_command():
    from recap.dsl.resource_builder import ResourceTemplateBuilder

    backend = RecordingBackend()
    context = object()
    with ResourceTemplateBuilder(
        backend=backend,
        namespace_id=uuid4(),
        namespace_path="beamline/amx",
        name="plate",
        type_names=["plate"],
        version="1.0",
        command_context=context,
    ) as builder:
        builder.add_properties(
            {"dimensions": [{"name": "rows", "type": "int", "default": 8}]}
        )
        builder.add_child("well", ["container", "well"])

    assert len(backend.commands) == 1
    command, submitted_context = backend.commands[0]
    assert isinstance(command, CreateResourceTemplate)
    assert submitted_context is context
    assert command.draft.children[0].name == "well"


def test_builder_submits_nothing_when_body_raises():
    from recap.dsl.resource_builder import ResourceTemplateBuilder

    backend = RecordingBackend()
    with (
        pytest.raises(RuntimeError, match="stop"),
        ResourceTemplateBuilder(
            backend=backend,
            namespace_id=uuid4(),
            namespace_path="beamline/amx",
            name="plate",
            type_names=["plate"],
            version="1.0",
            command_context=object(),
        ),
    ):
        raise RuntimeError("stop")
    assert backend.commands == []
