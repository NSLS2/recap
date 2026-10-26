import pytest

from .test_remote_builder_parity import command_client  # noqa: F401


def _run(client, suffix):
    client.create_namespace(suffix)
    scoped = client.namespace(suffix)
    with scoped.build_process_template("Transfers", "1.0") as template:
        for name in ("Transfer", "Repeat"):
            template.add_step(name).param_group("Transfer").add_attribute(
                "volume", "float", "uL", 10
            )
    with scoped.build_process_run("run", "desc", "Transfers", "1.0") as builder:
        pass
    return scoped, builder.process_run


def test_run_builder_templates_are_read_only(client):
    scoped, run = _run(client, "runtime-read-only")
    with scoped.build_process_run(process_run_id=run.id) as builder:
        with pytest.raises(TypeError, match="read-only"):
            builder.process_run.template.name = "unsaved"
        with pytest.raises(TypeError, match="read-only"):
            builder.process_run.steps["Transfer"].template.name = "unsaved"


def test_runtime_child_array_defaults_from_read_only_template(client):
    client.create_namespace("runtime-array-view")
    scoped = client.namespace("runtime-array-view")
    with scoped.build_process_template("ArrayRun", "1") as template:
        template.add_step("Collect").param_group("Readings").add_attribute(
            "samples", "array", "", [1, 2]
        )
    with scoped.build_process_run("run", "", "ArrayRun", "1") as builder:
        builder.save()
        child = builder.process_run.steps["Collect"].generate_child(name="repeat")
        assert child.parameters.readings["samples"].value == [1, 2]
        child.parameters.readings["samples"].value.append(3)
        builder.add_child_step(child)
    assert builder.process_run.steps["Collect"].children["repeat"].parameters.readings[
        "samples"
    ].value == [1, 2, 3]


def test_runtime_children_use_defaults_and_parent_scoped_names(client):
    scoped, run = _run(client, "runtime-children")
    with scoped.build_process_run(process_run_id=run.id) as builder:
        model = builder.get_model()
        model.steps["Transfer"].parameters.transfer.volume = 100
        builder.set_model(model)
        for parent_name in ("Transfer", "Repeat"):
            parent = builder.process_run.steps[parent_name]
            child = parent.generate_child(name="A01 to B01")
            assert child.parameters.transfer.volume.value == 10
            child.parameters.transfer.volume = 20
            builder.add_child_step(child)
    with scoped.build_process_run(process_run_id=run.id) as builder:
        result = builder.process_run
        assert set(result.steps) == {"Transfer", "Repeat"}
        for parent in result.steps.values():
            child = parent["A01 to B01"]
            assert child is parent.children["A01 to B01"]
            assert child.parent_id == parent.id
            assert child.template.id == parent.template.id
            assert child.parameters.transfer.volume.value == 20
        assert result.steps["Transfer"].parameters.transfer.volume.value == 100


def test_runtime_grandchildren_are_rejected(client):
    scoped, run = _run(client, "runtime-grandchildren")
    with scoped.build_process_run(process_run_id=run.id) as builder:
        child = builder.process_run.steps["Transfer"].generate_child(name="first")
        builder.add_child_step(child)
        with pytest.raises(ValueError, match="template-generated"):
            child.generate_child(name="second")


@pytest.mark.parametrize("in_place", [False, True])
def test_finalized_run_child_addition_and_edit(client, in_place):
    scoped, run = _run(client, f"runtime-lifecycle-{int(in_place)}")
    with scoped.build_process_run(process_run_id=run.id) as builder:
        builder.finalize()
    with scoped.build_process_run(process_run_id=run.id, in_place=in_place) as builder:
        builder.add_child_step(
            builder.process_run.steps["Transfer"].generate_child(name="first")
        )
    result = builder.process_run
    assert (result.id == run.id) is in_place
    assert result.steps["Transfer"]["first"].parent_id == result.steps["Transfer"].id
    with scoped.build_process_run(process_run_id=result.id, in_place=True) as builder:
        model = builder.get_model()
        model.steps["Transfer"]["first"].parameters.transfer.volume = 25
        builder.set_model(model)
    with scoped.build_process_run(process_run_id=result.id) as builder:
        assert (
            builder.process_run.steps["Transfer"][
                "first"
            ].parameters.transfer.volume.value
            == 25
        )


def test_child_step_resource_overrides_and_replacements(command_client):  # noqa: F811
    client = command_client
    client.create_namespace("runtime-overrides")
    scoped = client.namespace("runtime-overrides")
    with scoped.build_resource_template(name="Plate", type_names=["plate"]) as template:
        template.add_child("A01", ["well"])
    source = scoped.create_resource("source", "Plate")
    destination = scoped.create_resource("destination", "Plate")
    with scoped.build_process_template("Transfers", "1.0") as template:
        template.add_resource_slot("source_plate", "plate", "input")
        template.add_step("Transfer").bind_slot("source", "source_plate")
    with scoped.build_process_run("run", "desc", "Transfers", "1.0") as builder:
        builder.assign_resource("source_plate", source)
    run_id = builder.process_run.id
    with scoped.build_process_run(process_run_id=run_id) as builder:
        child = builder.process_run.steps["Transfer"].generate_child(name="first")
        assert child.resources["source"].id == source.id
        child.resources["source"] = source.children["A01"]
        builder.add_child_step(child)

    with scoped.build_process_run(process_run_id=run_id) as builder:
        model = builder.get_model()
        assert (
            model.steps["Transfer"]["first"].resources["source"].id
            == source.children["A01"].id
        )
        model.steps["Transfer"]["first"].resources["source"] = source
        builder.set_model(model)
    with scoped.build_process_run(process_run_id=run_id) as builder:
        assert (
            builder.process_run.steps["Transfer"]["first"].resources["source"].id
            == source.id
        )
    from recap.commands.errors import CommandValidationError
    from recap.exceptions import RecapValidationError

    with (
        pytest.raises((CommandValidationError, RecapValidationError)),
        scoped.build_process_run(process_run_id=run_id) as builder,
    ):
        child = builder.process_run.steps["Transfer"].generate_child(name="unrelated")
        child.resources["source"] = destination
        builder.add_child_step(child)


def test_duplicate_child_names_are_rejected_in_database(db_session):
    from sqlalchemy.exc import IntegrityError

    from recap.db.namespace import Namespace
    from recap.db.process import ProcessRun, ProcessTemplate
    from recap.db.step import Step, StepTemplate

    namespace = Namespace(path="step-uniqueness", metadata_json={})
    template = ProcessTemplate(name="Transfers", version="1", namespace=namespace)
    template.step_templates["Transfer"] = StepTemplate(name="Transfer")
    run = ProcessRun(name="run", description="", template=template, namespace=namespace)
    db_session.add(run)
    db_session.flush()
    parent = run.steps["Transfer"]
    for _ in range(2):
        with db_session.no_autoflush:
            child = Step(
                name="duplicate",
                template=parent.template,
                parent=parent,
                process_run=run,
            )
            db_session.add(child)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_internal_step_collection_uses_uuid_keys(db_session):
    from uuid import UUID

    from recap.db.namespace import Namespace
    from recap.db.process import ProcessRun, ProcessTemplate
    from recap.db.step import StepTemplate

    namespace = Namespace(path="step-identity", metadata_json={})
    template = ProcessTemplate(name="Transfers", version="1", namespace=namespace)
    template.step_templates["Transfer"] = StepTemplate(name="Transfer")
    run = ProcessRun(name="run", description="", template=template, namespace=namespace)
    db_session.add(run)
    db_session.flush()
    assert all(isinstance(key, UUID) for key in run.steps)
    assert run.steps["Transfer"] is run.steps[next(iter(run.steps))]


def test_resource_child_batch_rolls_back_parent_edit(command_client):  # noqa: F811
    from recap.commands.errors import CommandConflictError
    from recap.exceptions import RecapConflictError

    client = command_client
    client.create_namespace("atomic-children")
    scoped = client.namespace("atomic-children")
    with scoped.build_resource_template(name="Group", type_names=["group"]):
        pass
    root = scoped.create_resource("root", "Group")
    with scoped.build_resource(resource_id=root.id) as builder:
        builder.add_child("existing", "Group")
    with pytest.raises((CommandConflictError, RecapConflictError)):  # noqa: SIM117
        with scoped.build_resource(resource_id=root.id) as builder:
            model = builder.get_model()
            model.name = "changed"
            builder.set_model(model)
            builder.add_child("new", "Group")
            builder.add_child("existing", "Group")
    with scoped.build_resource(resource_id=root.id) as builder:
        assert builder.resource.name == "root"
        assert set(builder.resource.children) == {"existing"}


@pytest.mark.parametrize("in_place", [False, True])
def test_active_resource_child_addition_lifecycle(command_client, in_place):  # noqa: F811
    client = command_client
    client.create_namespace("active-children")
    scoped = client.namespace("active-children")
    with scoped.build_resource_template(name="Group", type_names=["group"]):
        pass
    root = scoped.create_resource("root", "Group")
    with scoped.build_resource(resource_id=root.id) as builder:
        builder.finalize()
    with scoped.build_resource(resource_id=root.id, in_place=in_place) as builder:
        builder.add_child("new", "Group")
    assert (builder.resource.id == root.id) is in_place
    with scoped.build_resource(resource_id=builder.resource.id) as reloaded:
        assert set(reloaded.resource.children) == {"new"}


@pytest.mark.parametrize("mutation", ["remove", "reparent", "template"])
def test_existing_child_structural_edits_are_rejected(client, mutation):
    scoped, run = _run(client, f"structural-{mutation}")
    with scoped.build_process_run(process_run_id=run.id) as builder:
        builder.add_child_step(
            builder.process_run.steps["Transfer"].generate_child(name="first")
        )
    with pytest.raises(ValueError, match="Child"):  # noqa: SIM117
        with scoped.build_process_run(process_run_id=run.id) as builder:
            model = builder.get_model()
            child = model.steps["Transfer"]["first"]
            if mutation == "remove":
                del model.steps["Transfer"].children["first"]
            elif mutation == "reparent":
                child.parent_id = model.steps["Repeat"].id
            else:
                child.template = model.steps["Repeat"].template
            builder.set_model(model)


def test_pending_child_changes_and_post_add_edits_are_persisted(client):
    scoped, run = _run(client, "pending-child")
    with scoped.build_process_run(process_run_id=run.id) as builder:
        child = builder.add_child_step(
            builder.process_run.steps["Transfer"].generate_child(name="first")
        )
        child.parameters.transfer.volume = 30
        assert (
            builder.changes()
            .fields["child_steps"][0]
            .parameters["Transfer"]["volume"]["value"]
            == 30
        )
    assert (
        builder.process_run.steps["Transfer"]["first"].parameters.transfer.volume.value
        == 30
    )


@pytest.mark.performance
def test_runtime_child_loading_has_bounded_statement_count(client):
    from .conftest import count_statements

    scoped, run = _run(client, "child-loading")
    with scoped.build_process_run(process_run_id=run.id) as builder:
        builder.add_child_step(
            builder.process_run.steps["Transfer"].generate_child(name="first")
        )
    engine = client.connection_state.backend.reader._session_factory.kw["bind"]
    with count_statements(engine) as small:
        scoped.build_process_run(process_run_id=run.id)
    with scoped.build_process_run(process_run_id=run.id) as builder:
        for index in range(96):
            builder.add_child_step(
                builder.process_run.steps["Transfer"].generate_child(
                    name=f"well-{index}"
                )
            )
    with count_statements(engine) as large:
        loaded = scoped.build_process_run(process_run_id=run.id)
    assert len(loaded.process_run.steps["Transfer"].children) == 97
    assert large["n"] <= small["n"] + 1


def test_child_addition_replays_without_duplicate(command_client):  # noqa: F811
    from dataclasses import replace

    from recap.commands.models import UpdateProcessRun
    from recap.dsl.drafts import ChildStepDraft

    scoped, run = _run(command_client, "child-replay")
    parent = run.steps["Transfer"]
    child = parent.generate_child(name="first")
    command = UpdateProcessRun(
        process_run_id=run.id,
        expected_revision=run.revision,
        child_steps=(
            ChildStepDraft(
                id=child.id,
                parent_id=parent.id,
                template_id=parent.template.id,
                name=child.name,
            ),
        ),
    )
    context = replace(scoped._command_context(), idempotency_key="child-replay")
    backend = scoped.connection_state.backend
    first = backend._execute(command, context)
    second = backend._execute(command, context)
    assert first.id == second.id
    assert first.revision == second.revision
    assert len(second.steps["Transfer"].children) == 1


def test_child_addition_stale_revision_rolls_back(command_client):  # noqa: F811
    from recap.commands.errors import CommandConflictError
    from recap.commands.models import UpdateProcessRun
    from recap.dsl.drafts import ChildStepDraft
    from recap.exceptions import RecapConflictError

    scoped, run = _run(command_client, "stale-child")
    stale = scoped.build_process_run(process_run_id=run.id)
    with scoped.build_process_run(process_run_id=run.id) as builder:
        builder.description = "current"
        model = builder.get_model()
        model.description = "current"
        builder.set_model(model)
    parent = stale.process_run.steps["Transfer"]
    child = parent.generate_child(name="first")
    command = UpdateProcessRun(
        process_run_id=run.id,
        expected_revision=1,
        child_steps=(
            ChildStepDraft(
                id=child.id,
                parent_id=parent.id,
                template_id=parent.template.id,
                name=child.name,
            ),
        ),
    )
    with pytest.raises((CommandConflictError, RecapConflictError)):
        scoped.connection_state.backend._execute(command, scoped._command_context())
    with scoped.build_process_run(process_run_id=run.id) as loaded:
        assert loaded.process_run.steps["Transfer"].children == {}


def test_step_name_migration_rejects_invalid_existing_hierarchy(tmp_path):
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, text

    migration = importlib.import_module(
        "recap.db.migrations.versions.a12f9d4b7e30_scope_step_names"
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE step (id TEXT PRIMARY KEY, name TEXT, parent_id TEXT, process_run_id TEXT, step_template_id TEXT)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO step VALUES ('root', 'Transfer', NULL, 'run', 'template'), ('child', 'first', 'root', 'other-run', 'template')"
            )
        )
        with (
            Operations.context(MigrationContext.configure(connection)),
            pytest.raises(ValueError, match="hierarchy"),
        ):
            migration.upgrade()
        connection.execute(
            text("UPDATE step SET process_run_id = 'run' WHERE id = 'child'")
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert connection.execute(
            text("SELECT name FROM step ORDER BY id")
        ).scalars().all() == ["first", "Transfer"]
