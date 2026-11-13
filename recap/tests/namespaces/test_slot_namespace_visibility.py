from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import RootModel
from sqlalchemy import func, select

from recap import RecapClient
from recap.authorization.scopes import Scope
from recap.commands.idempotency import command_fingerprint
from recap.commands.models import CopyProcessRun, CreateProcessRun, UpdateProcessRun
from recap.db.idempotency import IdempotencyRecord
from recap.db.resource import Resource
from recap.dsl.drafts import ProcessRunDraft
from recap.exceptions import RecapPermissionDeniedError, RecapValidationError
from recap.lifecycle import LifecycleStatus
from recap.schemas.process import ProcessRunCopyChanges, ProcessRunCopyOptions
from recap.schemas.resource import ResourceCopyOptions
from recap.tests.helpers.authorization import grant, policy


@pytest.fixture
def visibility_setup(command_client):  # noqa: F811
    base = f"visibility-{uuid4().hex}"
    paths = {
        "ancestor": base,
        "same": f"{base}/run",
        "sibling": f"{base}/other",
        "descendant": f"{base}/run/child",
        "prefix": f"{base}/runner",
    }
    resources = {}
    for path in paths.values():
        command_client.create_namespace(path)
    common = command_client.namespace(base)
    with common.build_resource_template(name="Plate", type_names=["plate"]) as template:
        template.add_child("A01", ["well"])
        template.add_properties(
            {"details": [{"name": "readings", "type": "array", "default": [1, 2]}]}
        )
        template.finalize()
    with common.build_process_template("Experiment", "1") as template:
        for slot in ("sample", "other"):
            template.add_resource_slot(slot, "plate", "input")
        template.add_step("Collect").bind_slot("sample", "sample").bind_slot(
            "other", "other"
        )
        template.finalize()
    template_id = template.template.id
    for key, path in paths.items():
        resources[key] = command_client.namespace(path).create_resource(key, "Plate")
    scoped = command_client.namespace(paths["same"])
    return scoped, paths, resources, template_id


def _create(scoped, template_id, assignments, copies=(), context=None):
    command = CreateProcessRun(
        namespace_path=scoped.namespace_path,
        draft=ProcessRunDraft(
            name="run",
            description="",
            template_id=template_id,
            assignments=assignments,
            copy_assignments=copies,
        ),
    )
    return scoped.connection_state.backend._execute(
        command, context or scoped._command_context()
    )


@pytest.mark.parametrize("source", ["same", "ancestor"])
def test_slot_accepts_visible_namespace(visibility_setup, source):
    scoped, _, resources, template_id = visibility_setup
    run = _create(scoped, template_id, {"sample": resources[source].id})
    assert run.assigned_resources["sample"].resource.id == resources[source].id


@pytest.mark.parametrize("source", ["sibling", "descendant", "prefix"])
def test_slot_rejects_nonvisible_namespace(visibility_setup, source):
    scoped, paths, resources, template_id = visibility_setup
    with pytest.raises(RecapValidationError, match="not visible") as error:
        _create(scoped, template_id, {"sample": resources[source].id})
    assert paths[source] in str(error.value)
    assert paths["same"] in str(error.value)


def test_slot_explicit_copy_preserves_tree(visibility_setup):
    scoped, _, resources, template_id = visibility_setup
    source = resources["sibling"]
    run = _create(scoped, template_id, {"sample": source.id}, ("sample",))
    copied = run.assigned_resources["sample"].resource
    assert copied.id != source.id
    assert copied.namespace_id == run.namespace_id
    assert copied.copied_from_id == source.id
    assert copied.children["A01"].id != source.children["A01"].id
    assert copied.properties.details.readings.value == [1, 2]


def test_visible_slot_copy_flag_is_noop(visibility_setup):
    scoped, _, resources, template_id = visibility_setup
    run = _create(scoped, template_id, {"sample": resources["same"].id}, ("sample",))
    assert run.assigned_resources["sample"].resource.id == resources["same"].id


def test_slot_copy_requires_root(visibility_setup):
    scoped, _, resources, template_id = visibility_setup
    with pytest.raises(RecapValidationError, match="resource graph root"):
        _create(
            scoped,
            template_id,
            {"sample": resources["sibling"].children["A01"].id},
            ("sample",),
        )


def test_slot_copy_rejects_unmatched_flag(visibility_setup):
    scoped, _, _, template_id = visibility_setup
    with pytest.raises((ValueError, RecapValidationError), match="copy_assignments"):
        _create(scoped, template_id, {}, ("sample",))


def test_slot_copy_new_child_refs_are_remapped_per_slot(visibility_setup):
    scoped, _, resources, template_id = visibility_setup
    run = _create(scoped, template_id, {})
    source = resources["sibling"]
    with scoped.build_process_run(process_run_id=run.id) as builder:
        # Pending role refinements still name source nodes; each slot must map
        # them to its own independent copy, not the last copy of that source.
        builder.assign_resource("sample", source, copy=True)
        builder.assign_resource("other", source, copy=True)
        child = builder.process_run.steps["Collect"].generate_child(name="one")
        child.resources["sample"] = source.children["A01"]
        child.resources["other"] = source.children["A01"]
        builder.add_child_step(child)
    result = builder.process_run
    a = result.assigned_resources["sample"].resource
    b = result.assigned_resources["other"].resource
    assert a.id != b.id
    assert a.copied_from_id == b.copied_from_id == source.id
    child = result.steps["Collect"]["one"]
    assert child.resources["sample"].id == a.children["A01"].id
    assert child.resources["other"].id == b.children["A01"].id


def test_builder_copy_flags_clear_after_save_and_plain_reassignment(visibility_setup):
    scoped, _, resources, _ = visibility_setup
    with scoped.build_process_run("run", "", "Experiment", "1") as builder:
        builder.assign_resource("sample", resources["sibling"], copy=True)
        builder.assign_resource("sample", resources["same"])
    assert (
        builder.process_run.assigned_resources["sample"].resource.id
        == resources["same"].id
    )
    assert not builder._copy_assignment_changes()


def test_standalone_cross_namespace_copy_round_trip(visibility_setup):
    scoped, _, resources, _ = visibility_setup
    source = resources["sibling"]
    copy = scoped.copy_resource(
        source.id, ResourceCopyOptions(allow_cross_namespace=True)
    )
    assert copy.namespace_id == scoped.namespace_context.id
    assert copy.copied_from_id == source.id


def test_run_copy_rejects_invisible_carryover_and_allows_replacement(visibility_setup):
    scoped, paths, resources, template_id = visibility_setup
    source = resources["same"]
    run = _create(scoped, template_id, {"sample": source.id})
    destination = RecapClient(
        connection_state=scoped.connection_state, namespace_path=paths["sibling"]
    )
    with pytest.raises(RecapValidationError, match="not visible"):
        destination.copy_process_run(run.id)
    copied = destination.copy_process_run(
        run.id,
        ProcessRunCopyOptions(
            changes=ProcessRunCopyChanges(
                assignments={"sample": resources["sibling"].id}
            )
        ),
    )
    assert copied.assigned_resources["sample"].resource.id == resources["sibling"].id


def test_run_copy_remaps_existing_child_refinements(visibility_setup):
    scoped, paths, resources, template_id = visibility_setup
    source = resources["same"]
    run = _create(scoped, template_id, {"sample": source.id})
    with scoped.build_process_run(process_run_id=run.id) as builder:
        child = builder.process_run.steps["Collect"].generate_child(name="one")
        child.resources["sample"] = source.children["A01"]
        builder.add_child_step(child)
    destination = RecapClient(
        connection_state=scoped.connection_state, namespace_path=paths["sibling"]
    )
    copied = destination.copy_process_run(
        run.id,
        ProcessRunCopyOptions(
            changes=ProcessRunCopyChanges(
                assignments={"sample": source.id}, copy_assignments=("sample",)
            )
        ),
    )
    copied_resource = copied.assigned_resources["sample"].resource
    assert copied_resource.namespace_id == copied.namespace_id
    assert (
        copied.steps["Collect"]["one"].resources["sample"].id
        == copied_resource.children["A01"].id
    )


def test_run_copy_into_descendant_preserves_visible_assignments(visibility_setup):
    scoped, paths, resources, template_id = visibility_setup
    run = _create(scoped, template_id, {"sample": resources["same"].id})
    copied = RecapClient(
        connection_state=scoped.connection_state, namespace_path=paths["descendant"]
    ).copy_process_run(run.id)
    assert copied.assigned_resources["sample"].resource.id == resources["same"].id


@pytest.mark.parametrize(
    "missing",
    [Scope.RESOURCE_READ, Scope.RESOURCE_WRITE, Scope.PROCESS_RUN_WRITE, None],
)
def test_copy_on_assign_requires_both_namespaces(client, missing):
    scoped, paths, resources, template_id = visibility_setup.__wrapped__(client)
    context = scoped._command_context()
    identity = context.actor.identities[0]
    grants = [grant(identity, paths["ancestor"], Scope.PROCESS_TEMPLATE_READ)]
    for path, scope in (
        (paths["sibling"], Scope.RESOURCE_READ),
        (paths["same"], Scope.RESOURCE_WRITE),
        (paths["same"], Scope.PROCESS_RUN_WRITE),
    ):
        if scope != missing:
            grants.append(grant(identity, path, scope))
    context = replace(context, policy=policy(*grants))
    if missing is None:
        run = _create(
            scoped,
            template_id,
            {"sample": resources["sibling"].id},
            ("sample",),
            context,
        )
        assert (
            run.assigned_resources["sample"].resource.namespace_id == run.namespace_id
        )
    else:
        with pytest.raises(RecapPermissionDeniedError):
            _create(
                scoped,
                template_id,
                {"sample": resources["sibling"].id},
                ("sample",),
                context,
            )


def test_copy_on_assign_rolls_back_clone_and_source_activation(client):
    scoped, _, resources, template_id = visibility_setup.__wrapped__(client)
    factory = client.connection_state.sessionmaker
    with factory() as session:
        before = session.scalar(select(func.count()).select_from(Resource))
        assert (
            session.get(Resource, resources["sibling"].id).status
            is LifecycleStatus.MUTABLE
        )
    with pytest.raises(RecapValidationError):
        _create(
            scoped,
            template_id,
            {"sample": resources["sibling"].id, "unknown": resources["same"].id},
            ("sample",),
        )
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Resource)) == before
        assert (
            session.get(Resource, resources["sibling"].id).status
            is LifecycleStatus.MUTABLE
        )


def test_copy_on_assign_idempotent_create_replay(visibility_setup):
    scoped, _, resources, template_id = visibility_setup
    context = replace(scoped._command_context(), idempotency_key=f"copy-{uuid4().hex}")
    first = _create(
        scoped, template_id, {"sample": resources["sibling"].id}, ("sample",), context
    )
    second = _create(
        scoped, template_id, {"sample": resources["sibling"].id}, ("sample",), context
    )
    assert first.id == second.id
    assert (
        first.assigned_resources["sample"].resource.id
        == second.assigned_resources["sample"].resource.id
    )


def test_copy_on_assign_idempotent_update_replay(visibility_setup):
    scoped, _, resources, template_id = visibility_setup
    run = _create(scoped, template_id, {})
    context = replace(scoped._command_context(), idempotency_key=f"copy-{uuid4().hex}")
    command = UpdateProcessRun(
        process_run_id=run.id,
        expected_revision=run.revision,
        assignments={"sample": resources["sibling"].id},
        copy_assignments=("sample",),
    )
    first = scoped.connection_state.backend._execute(command, context)
    second = scoped.connection_state.backend._execute(command, context)
    assert first.revision == second.revision
    assert (
        first.assigned_resources["sample"].resource.id
        == second.assigned_resources["sample"].resource.id
    )


def test_builder_can_copy_legacy_active_assignment_with_unchanged_id(client):
    scoped, _, resources, template_id = visibility_setup.__wrapped__(client)
    run = _create(scoped, template_id, {})
    # Model legacy data directly: assigning invisible resources was previously allowed.
    from recap.db.process import ProcessRun

    with client.connection_state.sessionmaker.begin() as session:
        stored = session.get(ProcessRun, run.id)
        slot = next(
            slot for slot in stored.template.resource_slots if slot.name == "sample"
        )
        stored.resources[slot] = session.get(Resource, resources["sibling"].id)
        stored.finalize()
    with scoped.build_process_run(process_run_id=run.id) as builder:
        builder.assign_resource("sample", resources["sibling"], copy=True)
    assert builder.process_run.id != run.id
    assert (
        builder.process_run.assigned_resources["sample"].resource.namespace_id
        == run.namespace_id
    )


def test_update_rejects_unmatched_copy_flags(visibility_setup):
    scoped, _, _, template_id = visibility_setup
    run = _create(scoped, template_id, {})
    command = UpdateProcessRun(
        process_run_id=run.id,
        expected_revision=run.revision,
        description="changed",
        copy_assignments=("sample",),
    )
    with pytest.raises(RecapValidationError, match="copy_assignments"):
        scoped.connection_state.backend._execute(command, scoped._command_context())


def test_unflagged_create_preserves_legacy_fingerprint(client):
    scoped, _, _, template_id = visibility_setup.__wrapped__(client)
    key = f"legacy-{uuid4().hex}"
    context = replace(scoped._command_context(), idempotency_key=key)
    _create(scoped, template_id, {}, context=context)
    expected = command_fingerprint(
        method="POST",
        route_template="/api/v1/process-runs/{namespace_path:path}",
        namespace_path=scoped.namespace_path,
        source_id=None,
        body=RootModel(
            {
                "id": None,
                "name": "run",
                "description": "",
                "template_id": str(template_id),
                "assignments": {},
                "steps": {},
                "child_steps": [],
            }
        ),
    )
    with client.connection_state.sessionmaker() as session:
        record = session.get(IdempotencyRecord, (context.actor.actor_id, key))
        assert record.fingerprint == expected


def test_copy_on_assign_type_mismatch_is_validation_error(visibility_setup):
    scoped, paths, _, template_id = visibility_setup
    sibling = RecapClient(
        connection_state=scoped.connection_state, namespace_path=paths["sibling"]
    )
    with sibling.build_resource_template(name="Wrong", type_names=["wrong"]):
        pass
    resource = sibling.create_resource("wrong", "Wrong")
    with pytest.raises(RecapValidationError, match="required type"):
        _create(scoped, template_id, {"sample": resource.id}, ("sample",))


def test_copy_on_assign_archived_source_allowed(client):
    scoped, _, resources, template_id = visibility_setup.__wrapped__(client)
    with client.connection_state.sessionmaker.begin() as session:
        session.get(Resource, resources["sibling"].id).status = LifecycleStatus.ARCHIVED
    run = _create(scoped, template_id, {"sample": resources["sibling"].id}, ("sample",))
    assert run.assigned_resources["sample"].resource.status is LifecycleStatus.MUTABLE


@pytest.mark.parametrize("operation", ["create", "update", "copy"])
@pytest.mark.parametrize("revoked", [Scope.RESOURCE_READ, Scope.RESOURCE_WRITE])
def test_flagged_run_replay_rechecks_resource_permissions(client, operation, revoked):
    scoped, paths, resources, template_id = visibility_setup.__wrapped__(client)
    source = resources["sibling"]
    if operation == "create":
        command = CreateProcessRun(
            namespace_path=scoped.namespace_path,
            draft=ProcessRunDraft(
                name="run",
                description="",
                template_id=template_id,
                assignments={"sample": source.id},
                copy_assignments=("sample",),
            ),
        )
    elif operation == "update":
        run = _create(scoped, template_id, {})
        command = UpdateProcessRun(
            process_run_id=run.id,
            expected_revision=run.revision,
            assignments={"sample": source.id},
            copy_assignments=("sample",),
        )
    else:
        sibling = RecapClient(
            connection_state=scoped.connection_state, namespace_path=paths["sibling"]
        )
        run = _create(sibling, template_id, {"sample": source.id})
        command = CopyProcessRun(
            source_process_run_id=run.id,
            destination_namespace_path=scoped.namespace_path,
            options=ProcessRunCopyOptions(
                changes=ProcessRunCopyChanges(
                    assignments={"sample": source.id}, copy_assignments=("sample",)
                )
            ),
        )
    context = replace(
        scoped._command_context(), idempotency_key=f"replay-{uuid4().hex}"
    )
    identity = context.actor.identities[0]
    permissions = (
        (paths["same"], Scope.PROCESS_RUN_WRITE),
        (paths["sibling"], Scope.PROCESS_RUN_READ),
        (paths["ancestor"], Scope.PROCESS_TEMPLATE_READ),
        (paths["same"], Scope.RESOURCE_WRITE),
        (paths["sibling"], Scope.RESOURCE_READ),
    )
    context = replace(
        context,
        policy=policy(*(grant(identity, path, scope) for path, scope in permissions)),
    )
    scoped.connection_state.backend._execute(command, context)
    revoked_context = replace(
        context,
        policy=policy(
            *(
                grant(identity, path, scope)
                for path, scope in permissions
                if scope != revoked
            )
        ),
    )
    with pytest.raises(RecapPermissionDeniedError):
        scoped.connection_state.backend._execute(command, revoked_context)


def test_remapped_child_requires_only_documented_copy_permissions(client):
    scoped, paths, resources, template_id = visibility_setup.__wrapped__(client)
    source = resources["same"]
    run = _create(scoped, template_id, {"sample": source.id})
    with scoped.build_process_run(process_run_id=run.id) as builder:
        child = builder.process_run.steps["Collect"].generate_child(name="one")
        child.resources["sample"] = source.children["A01"]
        builder.add_child_step(child)
    context = scoped._command_context()
    identity = context.actor.identities[0]
    context = replace(
        context,
        policy=policy(
            grant(identity, paths["same"], Scope.PROCESS_RUN_READ),
            grant(identity, paths["same"], Scope.RESOURCE_READ),
            grant(identity, paths["sibling"], Scope.PROCESS_RUN_WRITE),
            grant(identity, paths["sibling"], Scope.RESOURCE_WRITE),
        ),
    )
    copied = scoped.connection_state.backend._execute(
        CopyProcessRun(
            source_process_run_id=run.id,
            destination_namespace_path=paths["sibling"],
            options=ProcessRunCopyOptions(
                changes=ProcessRunCopyChanges(
                    assignments={"sample": source.id}, copy_assignments=("sample",)
                )
            ),
        ),
        context,
    )
    assert (
        copied.steps["Collect"]["one"].resources["sample"].id
        == copied.assigned_resources["sample"].resource.children["A01"].id
    )


def test_run_copy_explicit_override_replaces_carried_refinement(visibility_setup):
    scoped, paths, resources, template_id = visibility_setup
    source = resources["same"]
    run = _create(scoped, template_id, {"sample": source.id})
    with scoped.build_process_run(process_run_id=run.id) as builder:
        child = builder.process_run.steps["Collect"].generate_child(name="one")
        child.resources["sample"] = source.children["A01"]
        builder.add_child_step(child)
    target = resources["sibling"]
    destination = RecapClient(
        connection_state=scoped.connection_state, namespace_path=paths["sibling"]
    )
    copied = destination.copy_process_run(
        run.id,
        ProcessRunCopyOptions(
            changes=ProcessRunCopyChanges(
                assignments={"sample": target.id},
                steps={
                    str(child.id): {"resources": {"sample": target.children["A01"].id}}
                },
            )
        ),
    )
    assert (
        copied.steps["Collect"]["one"].resources["sample"].id
        == target.children["A01"].id
    )
