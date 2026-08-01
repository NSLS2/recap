from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from recap.authentication.models import ActorKind, ProviderIdentity, RequestActor
from recap.authorization.policy import UnrestrictedNamespacePolicy
from recap.authorization.scopes import Scope
from recap.commands.errors import CommandConflictError
from recap.commands.models import CommandContext
from recap.commands.service import CommandService
from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.db.base import Base
from recap.db.namespace import Namespace
from recap.db.resource import Resource, ResourceTemplate
from recap.lifecycle import LifecycleStatus
from recap.server.audit import AuditOutcome


class AuditCollector:
    def __init__(self):
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def command_setup(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'resources.db'}")
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
        idempotency_key="resource-command-1",
    )
    return CommandService(factory), factory, context, audit


def _template(factory, path="beamline"):
    with factory.begin() as session:
        namespace = Namespace(path=path)
        template = ResourceTemplate(name="plate", version="1", namespace=namespace)
        session.add(template)
        session.flush()
        return namespace.path, template.id


def _property_template(factory, path="beamline/properties"):
    with factory.begin() as session:
        namespace = Namespace(path=path)
        template = ResourceTemplate(
            name="property-plate", version="1", namespace=namespace
        )
        group = AttributeGroupTemplate(name="measurements", resource_template=template)
        AttributeTemplate(
            name="dose",
            value_type="int",
            unit="mg",
            default_value=0,
            attribute_group_template=group,
        )
        session.add(template)
        session.flush()
        return namespace.path, template.id


def test_resource_create_update_revision_and_frozen_patch(command_setup):
    service, factory, context, audit = command_setup
    path, template_id = _template(factory)

    created = service.create_resource(
        context, namespace_path=path, name="plate-1", template_id=template_id
    )
    updated = service.update_resource(
        replace(context, idempotency_key="resource-command-2"),
        resource_id=created.id,
        expected_revision=1,
        name="plate-2",
    )
    assert updated.name == "plate-2"
    assert updated.revision == 2

    with factory.begin() as session:
        session.get(Resource, created.id).activate()

    with pytest.raises(CommandConflictError, match="active resource"):
        service.update_resource(
            replace(context, idempotency_key="resource-command-3"),
            resource_id=created.id,
            expected_revision=2,
            name="plate-3",
        )
    with factory() as session:
        assert (
            session.scalar(select(Resource).where(Resource.id == created.id)).status
            is LifecycleStatus.ACTIVE
        )
    assert audit.records[-1].outcome is AuditOutcome.ERROR


def test_resource_create_and_update_apply_complete_property_payload(command_setup):
    service, _, context, _ = command_setup
    path, template_id = _property_template(command_setup[1])
    created = service.create_resource(
        context,
        namespace_path=path,
        name="property-plate-1",
        template_id=template_id,
        properties={
            "measurements": {
                "dose": {
                    "value": 12,
                    "unit": "mg",
                    "metadata_json": {"source": "builder"},
                }
            }
        },
    )
    assert created.properties["measurements"].values["dose"].value == 12
    assert created.properties["measurements"].values["dose"].metadata_json == {
        "source": "builder"
    }

    updated = service.update_resource(
        replace(context, idempotency_key="resource-property-update"),
        resource_id=created.id,
        expected_revision=1,
        properties={"measurements": {"dose": {"value": 24}}},
    )
    assert updated.properties["measurements"].values["dose"].value == 24
