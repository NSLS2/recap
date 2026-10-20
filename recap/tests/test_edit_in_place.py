"""Explicit in-place edits of ACTIVE resources and process runs."""

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from recap.authentication.models import ActorKind, ProviderIdentity, RequestActor
from recap.authorization.policy import UnrestrictedNamespacePolicy
from recap.authorization.scopes import Scope
from recap.commands.errors import CommandConflictError
from recap.commands.idempotency import command_fingerprint
from recap.commands.models import CommandContext, UpdateProcessRun
from recap.commands.service import CommandService
from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.db.base import Base
from recap.db.idempotency import IdempotencyRecord
from recap.db.namespace import Namespace
from recap.db.resource import Resource, ResourceTemplate
from recap.dsl.query import QuerySpec
from recap.exceptions import RecapConflictError, RecapNotFoundError
from recap.lifecycle import LifecycleStatus
from recap.schemas.resource import ResourceSchema
from recap.tests.test_process_run_update import _assignment_fixture


def _resource_tree(db_session, suffix):
    namespace = Namespace(path=f"/in-place-{suffix}", status=LifecycleStatus.ACTIVE)
    value_template = AttributeTemplate(
        name="value", value_type="int", default_value="1"
    )
    group = AttributeGroupTemplate(
        name="properties", attribute_templates=[value_template]
    )
    template = ResourceTemplate(
        name=f"resource-{suffix}",
        version="1",
        namespace=namespace,
        attribute_group_templates=[group],
    )
    root = Resource(name=f"root-{suffix}", template=template, namespace=namespace)
    child = Resource(
        name=f"child-{suffix}", template=template, namespace=namespace, parent=root
    )
    db_session.add(root)
    db_session.flush()
    return root, child


def test_guard_allows_registered_root_aggregate(db_session):
    from recap.db.process import allow_in_place_edit

    root, child = _resource_tree(db_session, "allowed")
    root.activate()
    db_session.flush()

    allow_in_place_edit(db_session, root)
    root.name = "renamed-root"
    child.properties["properties"].values["value"] = 2
    db_session.flush()

    assert root.status == LifecycleStatus.ACTIVE
    assert child.properties["properties"].values["value"] == 2


def test_guard_still_blocks_unregistered_active_aggregate(db_session):
    from recap.db.process import allow_in_place_edit

    allowed_root, _ = _resource_tree(db_session, "first")
    other_root, other_child = _resource_tree(db_session, "second")
    allowed_root.activate()
    other_root.activate()
    db_session.flush()

    allow_in_place_edit(db_session, allowed_root)
    allowed_root.name = "renamed-first"
    other_child.properties["properties"].values["value"] = 3

    with pytest.raises(ValueError, match="active resource"):
        db_session.flush()


def test_guard_still_validates_status_transition_for_registered_root(db_session):
    from recap.db.process import allow_in_place_edit

    root, _ = _resource_tree(db_session, "transition")
    root.activate()
    db_session.flush()

    allow_in_place_edit(db_session, root)
    root.status = LifecycleStatus.MUTABLE

    with pytest.raises(ValueError, match="Invalid lifecycle transition"):
        db_session.flush()


def test_guard_blocks_registered_archived_root(db_session):
    from recap.db.process import allow_in_place_edit

    root, _ = _resource_tree(db_session, "archived")
    root.activate()
    db_session.flush()
    root.archive()
    db_session.flush()

    allow_in_place_edit(db_session, root)
    root.name = "renamed-archived"

    with pytest.raises(ValueError, match="active resource"):
        db_session.flush()


def test_allow_in_place_edit_requires_persisted_root(db_session):
    from recap.db.process import allow_in_place_edit

    root = Resource(name="unflushed")
    assert root.id is None

    with pytest.raises(ValueError, match="persisted aggregate root"):
        allow_in_place_edit(db_session, root)


class _AuditCollector:
    def __init__(self):
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def service_setup(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'in-place.db'}")
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
    context = CommandContext(
        actor=actor,
        request_id=str(uuid4()),
        policy=UnrestrictedNamespacePolicy(),
        audit_sink=_AuditCollector(),
        authorization_generation=None,
        idempotency_key="in-place-1",
    )
    with factory.begin() as session:
        namespace = Namespace(path="beamline")
        template = ResourceTemplate(name="plate", version="1", namespace=namespace)
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
        template_id = template.id
    return CommandService(factory), factory, context, template_id


def _key(context, key):
    return replace(context, idempotency_key=key)


def _create(service, context, template_id, name, parent_id=None):
    return service.create_resource(
        _key(context, f"create-{name}"),
        namespace_path="beamline",
        name=name,
        template_id=template_id,
        parent_id=parent_id,
    )


def _set_status(factory, resource_id, status):
    with factory.begin() as session:
        resource = session.get(Resource, resource_id)
        if status is LifecycleStatus.ACTIVE:
            resource.activate()
        else:
            resource.archive()
        return resource.revision


def test_service_in_place_updates_active_root(service_setup):
    service, factory, context, template_id = service_setup
    created = _create(service, context, template_id, "plate-1")
    revision = _set_status(factory, created.id, LifecycleStatus.ACTIVE)

    updated = service.update_resource(
        _key(context, "in-place-root"),
        resource_id=created.id,
        expected_revision=revision,
        name="plate-1-fixed",
        properties={"measurements": {"dose": {"value": 5}}},
        in_place=True,
    )

    assert updated.id == created.id
    assert updated.name == "plate-1-fixed"
    assert updated.properties["measurements"].values["dose"].value == 5
    assert updated.status == LifecycleStatus.ACTIVE
    assert updated.revision == revision + 1
    with factory() as session:
        copies = session.scalars(
            select(Resource).where(Resource.copied_from_id == created.id)
        ).all()
    assert copies == []


def test_service_in_place_updates_child_of_active_root(service_setup):
    service, factory, context, template_id = service_setup
    root = _create(service, context, template_id, "root")
    child = _create(service, context, template_id, "child", parent_id=root.id)
    _set_status(factory, root.id, LifecycleStatus.ACTIVE)

    with pytest.raises(CommandConflictError, match="active resource"):
        service.update_resource(
            _key(context, "child-no-flag"),
            resource_id=child.id,
            expected_revision=child.revision,
            properties={"measurements": {"dose": {"value": 7}}},
        )

    updated = service.update_resource(
        _key(context, "child-in-place"),
        resource_id=child.id,
        expected_revision=child.revision,
        properties={"measurements": {"dose": {"value": 7}}},
        in_place=True,
    )
    assert updated.properties["measurements"].values["dose"].value == 7
    assert updated.revision == child.revision + 1


def test_service_in_place_rejects_archived_resource(service_setup):
    service, factory, context, template_id = service_setup
    created = _create(service, context, template_id, "archived")
    _set_status(factory, created.id, LifecycleStatus.ACTIVE)
    revision = _set_status(factory, created.id, LifecycleStatus.ARCHIVED)

    with pytest.raises(CommandConflictError, match="archived resource"):
        service.update_resource(
            _key(context, "archived-in-place"),
            resource_id=created.id,
            expected_revision=revision,
            name="nope",
            in_place=True,
        )


def test_service_in_place_rejects_stale_revision(service_setup):
    service, factory, context, template_id = service_setup
    created = _create(service, context, template_id, "stale")
    _set_status(factory, created.id, LifecycleStatus.ACTIVE)

    with pytest.raises(CommandConflictError, match="stale"):
        service.update_resource(
            _key(context, "stale-in-place"),
            resource_id=created.id,
            expected_revision=1,
            name="stale-fixed",
            in_place=True,
        )


def test_service_in_place_is_part_of_idempotency_fingerprint(service_setup):
    service, _, context, template_id = service_setup
    created = _create(service, context, template_id, "idem")
    service.update_resource(
        _key(context, "shared-key"),
        resource_id=created.id,
        expected_revision=1,
        name="idem-2",
    )

    with pytest.raises(CommandConflictError, match="Idempotency key"):
        service.update_resource(
            _key(context, "shared-key"),
            resource_id=created.id,
            expected_revision=1,
            name="idem-2",
            in_place=True,
        )


class _PreFeatureUpdateResourceBody(BaseModel):
    name: str | None
    properties: dict[str, dict[str, object]] | None
    expected_revision: int


class _PreFeatureUpdateProcessRunBody(BaseModel):
    description: str | None
    status: str | None
    assignments: dict[str, UUID] | None
    steps: dict[str, dict[str, dict[str, object]]] | None
    expected_revision: int


def _stored_fingerprint(factory, context, key):
    with factory() as session:
        return session.scalar(
            select(IdempotencyRecord.fingerprint).where(
                IdempotencyRecord.actor_id == context.actor.actor_id,
                IdempotencyRecord.idempotency_key == key,
            )
        )


def _resource_update_fingerprint(resource_id, body):
    return command_fingerprint(
        method="PATCH",
        route_template="/api/v1/resources/{resource_id}",
        namespace_path=None,
        source_id=resource_id,
        body=body,
    )


def _process_run_update_fingerprint(process_run_id, body):
    return command_fingerprint(
        method="PATCH",
        route_template="/api/v1/process-runs/{process_run_id}",
        namespace_path=None,
        source_id=process_run_id,
        body=body,
    )


def test_default_resource_update_fingerprint_matches_pre_feature_shape(
    service_setup,
):
    service, factory, context, template_id = service_setup
    created = _create(service, context, template_id, "fp-default")
    service.update_resource(
        _key(context, "fp-default-update"),
        resource_id=created.id,
        expected_revision=1,
        name="fp-default-2",
    )

    expected = _resource_update_fingerprint(
        created.id,
        _PreFeatureUpdateResourceBody(
            name="fp-default-2", properties=None, expected_revision=1
        ),
    )
    assert _stored_fingerprint(factory, context, "fp-default-update") == expected


def test_in_place_resource_update_fingerprint_differs_from_default(service_setup):
    service, factory, context, template_id = service_setup
    created = _create(service, context, template_id, "fp-in-place")
    service.update_resource(
        _key(context, "fp-in-place-update"),
        resource_id=created.id,
        expected_revision=1,
        name="fp-in-place-2",
        in_place=True,
    )

    default = _resource_update_fingerprint(
        created.id,
        _PreFeatureUpdateResourceBody(
            name="fp-in-place-2", properties=None, expected_revision=1
        ),
    )
    stored = _stored_fingerprint(factory, context, "fp-in-place-update")
    assert stored is not None
    assert stored != default


def _mutable_run(factory):
    from recap.db.process import ProcessRun, ProcessTemplate

    with factory.begin() as session:
        namespace = session.scalar(
            select(Namespace).where(Namespace.path == "beamline")
        )
        template = ProcessTemplate(namespace=namespace, name="fp-run", version="1")
        run = ProcessRun(
            namespace=namespace, name="fp-run", description="d", template=template
        )
        session.add_all([template, run])
        session.flush()
        return run.id, run.revision


@pytest.mark.parametrize("in_place", [False, True])
def test_process_run_update_fingerprint_pre_feature_compatibility(
    service_setup, in_place
):
    service, factory, context, _ = service_setup
    run_id, revision = _mutable_run(factory)
    key = f"fp-run-{in_place}"
    service.update_process_run(
        _key(context, key),
        process_run_id=run_id,
        expected_revision=revision,
        description="new",
        in_place=in_place,
    )

    pre_feature = _process_run_update_fingerprint(
        run_id,
        _PreFeatureUpdateProcessRunBody(
            description="new",
            status=None,
            assignments=None,
            steps=None,
            expected_revision=revision,
        ),
    )
    stored = _stored_fingerprint(factory, context, key)
    assert stored is not None
    assert (stored == pre_feature) is not in_place


def test_service_in_place_on_mutable_resource_is_normal_update(service_setup):
    service, _, context, template_id = service_setup
    created = _create(service, context, template_id, "mutable")

    updated = service.update_resource(
        _key(context, "mutable-in-place"),
        resource_id=created.id,
        expected_revision=1,
        name="mutable-2",
        in_place=True,
    )

    assert updated.status == LifecycleStatus.MUTABLE
    assert updated.revision == 2


def _active_run_with_archived_assignment(factory):
    """ACTIVE run whose ``held`` slot holds a since-archived resource."""
    from recap.db.process import Direction, ProcessRun, ProcessTemplate, ResourceSlot
    from recap.db.resource import ResourceType

    with factory.begin() as session:
        namespace = session.scalar(
            select(Namespace).where(Namespace.path == "beamline")
        )
        resource_type = ResourceType(name="sample")
        process_template = ProcessTemplate(
            namespace=namespace, name="collect", version="1"
        )
        for slot_name in ("held", "extra"):
            ResourceSlot(
                name=slot_name,
                process_template=process_template,
                resource_type=resource_type,
                direction=Direction.input,
            )
        sample_template = ResourceTemplate(
            namespace=namespace, name="sample", version="1", types=[resource_type]
        )
        held, fresh, archived = (
            Resource(namespace=namespace, name=name, template=sample_template)
            for name in ("held", "fresh", "archived")
        )
        run = ProcessRun(
            namespace=namespace,
            name="run-1",
            description="desc",
            template=process_template,
        )
        session.add_all([process_template, held, fresh, archived, run])
        session.flush()
        run.resources[process_template.resource_slots[0]] = held
        session.flush()
        run.finalize()
        for resource in (held, archived):
            resource.activate()
        session.flush()
        for resource in (held, archived):
            resource.archive()
        session.flush()
        return run.id, run.revision, fresh.id, archived.id


def test_service_in_place_run_rejects_assigning_archived_resource(service_setup):
    service, factory, context, _ = service_setup
    run_id, revision, _, archived_id = _active_run_with_archived_assignment(factory)

    with pytest.raises(CommandConflictError, match="archived resource"):
        service.update_process_run(
            _key(context, "assign-archived"),
            process_run_id=run_id,
            expected_revision=revision,
            assignments={"extra": archived_id},
            in_place=True,
        )


def test_service_in_place_run_ignores_previously_archived_assignment(service_setup):
    service, factory, context, _ = service_setup
    run_id, revision, fresh_id, _ = _active_run_with_archived_assignment(factory)

    service.update_process_run(
        _key(context, "assign-fresh"),
        process_run_id=run_id,
        expected_revision=revision,
        assignments={"extra": fresh_id},
        in_place=True,
    )

    with factory() as session:
        assert session.get(Resource, fresh_id).status is LifecycleStatus.ACTIVE


def _active_robot(client, suffix):
    client.create_namespace(suffix)
    scoped = client.namespace(suffix)
    with scoped.build_resource_template(
        name=f"Robot-{suffix}", type_names=["instrument"]
    ) as rtb:
        rtb.prop_group("Details").add_attribute(
            "serial", "str", "", "abc"
        ).close_group()
    resource = scoped.create_resource(f"robot-{suffix}", f"Robot-{suffix}")
    with scoped.build_resource(resource_id=resource.id) as builder:
        builder.finalize()
    return scoped, builder.resource


def _resources_named(client, scoped, name):
    return client.connection_state.backend.query(
        ResourceSchema,
        QuerySpec(filters={"name": name}, include_mutable=True),
        namespace_path=scoped.namespace_context.path,
    )


def test_builder_in_place_edits_active_resource(client):
    scoped, active = _active_robot(client, "builder-in-place")

    with scoped.build_resource(resource_id=active.id, in_place=True) as builder:
        builder.resource.properties.details.values.serial.value = "xyz"

    assert builder.resource.id == active.id
    assert builder.resource.status == LifecycleStatus.ACTIVE
    assert builder.resource.revision == active.revision + 1
    assert builder.resource.properties.details.values.serial.value == "xyz"
    with scoped.build_resource(resource_id=active.id) as reloaded:
        assert reloaded.resource.properties.details.values.serial.value == "xyz"
    assert len(_resources_named(client, scoped, active.name)) == 1


def test_builder_without_in_place_still_copies_active_resource(client):
    scoped, active = _active_robot(client, "builder-copy")

    with scoped.build_resource(resource_id=active.id) as builder:
        builder.resource.properties.details.values.serial.value = "xyz"

    assert builder.resource.id != active.id
    assert builder.resource.copied_from_id == active.id
    with scoped.build_resource(resource_id=active.id) as source:
        assert source.resource.properties.details.values.serial.value == "abc"


def test_builder_in_place_edits_child_of_active_resource(client):
    scoped, _ = _active_robot(client, "builder-child")
    parent = scoped.create_resource("parent-builder-child", "Robot-builder-child")
    with scoped.build_resource(resource_id=parent.id) as builder:
        child_builder = builder.add_child("child", "Robot-builder-child")
    child_id = child_builder.resource.id
    with scoped.build_resource(resource_id=parent.id) as builder:
        builder.finalize()

    with scoped.build_resource(resource_id=child_id, in_place=True) as builder:
        builder.resource.properties.details.values.serial.value = "child-xyz"

    assert builder.resource.id == child_id
    with scoped.build_resource(resource_id=child_id) as reloaded:
        assert reloaded.resource.properties.details.values.serial.value == "child-xyz"


def test_builder_in_place_rejects_adding_child_to_active_resource(client):
    scoped, active = _active_robot(client, "builder-add-child")

    # In-place covers edits only; structural additions hit the active-aggregate
    # guard. The parent's own UpdateResource may already have committed.
    with (
        pytest.raises(ValueError, match="Cannot mutate an active resource aggregate"),
        scoped.build_resource(resource_id=active.id, in_place=True) as builder,
    ):
        builder.add_child("new-child", "Robot-builder-add-child")

    with scoped.build_resource(resource_id=active.id) as reloaded:
        assert reloaded.resource.id == active.id
        assert reloaded.resource.status == LifecycleStatus.ACTIVE
        assert reloaded.resource.children == {}


def _finalized_run(client, suffix):
    client.create_namespace(suffix)
    scoped = client.namespace(suffix)
    with scoped.build_process_template(f"PT-{suffix}", "1.0") as ptb:
        (
            ptb.add_step("Mix")
            .param_group("Inputs")
            .add_attribute("Voltage", "int", "", "0")
            .close_group()
            .close_step()
        )
    with scoped.build_process_run(
        f"run-{suffix}", "original", f"PT-{suffix}", "1.0"
    ) as builder:
        builder.finalize()
    return scoped, builder.process_run


def test_builder_in_place_edits_finalized_run_params_and_description(client):
    scoped, run = _finalized_run(client, "run-in-place")

    with scoped.build_process_run(process_run_id=run.id, in_place=True) as builder:
        params = builder.get_params("Mix")
        params.inputs.values.voltage.value = 42
        builder.set_params(params)
    assert builder.process_run.id == run.id
    assert builder.process_run.revision == run.revision + 1

    with scoped.build_process_run(process_run_id=run.id, in_place=True) as builder:
        model = builder.get_model()
        model.description = "corrected"
        builder.set_model(model)

    assert builder.process_run.id == run.id
    assert builder.process_run.status == LifecycleStatus.ACTIVE
    assert builder.process_run.revision == run.revision + 2
    with scoped.build_process_run(process_run_id=run.id) as reloaded:
        loaded = reloaded.process_run
    assert loaded.description == "corrected"
    assert loaded.steps["Mix"].parameters.inputs.values.voltage.value == 42


def test_builder_in_place_assignment_activates_resource(client):
    scoped, unchanged, changed = _assignment_fixture(client, "in-place-assign")
    with scoped.build_process_run(
        "run", "desc", "Pandda-in-place-assign", "1.0"
    ) as builder:
        builder.assign_resource("unchanged_dataset", unchanged)
        builder.finalize()
    run = builder.process_run

    with scoped.build_process_run(process_run_id=run.id, in_place=True) as builder:
        builder.assign_resource("changed_dataset", changed)

    assert builder.process_run.id == run.id
    assert {
        name: assignment.resource.id
        for name, assignment in builder.process_run.assigned_resources.items()
    } == {"unchanged_dataset": unchanged.id, "changed_dataset": changed.id}
    with scoped.build_resource(resource_id=changed.id) as resource_builder:
        assert resource_builder.resource.status == LifecycleStatus.ACTIVE


def test_builder_in_place_rejects_archived_run(client):
    scoped, run = _finalized_run(client, "run-archived")
    with scoped.build_process_run(process_run_id=run.id) as builder:
        builder.archive()

    # Builders load runs with include_mutable only, so an archived run cannot
    # even be opened (pre-existing behaviour, independent of in_place).
    with pytest.raises(RecapNotFoundError):
        scoped.build_process_run(process_run_id=run.id, in_place=True)

    # The in-place command the builder would send is rejected by the backend.
    with pytest.raises(RecapConflictError, match="archived process run"):
        client.connection_state.backend._execute(
            UpdateProcessRun(
                process_run_id=run.id,
                expected_revision=builder.process_run.revision,
                description="too late",
                in_place=True,
            ),
            scoped._command_context(),
        )
