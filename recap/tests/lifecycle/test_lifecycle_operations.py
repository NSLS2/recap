from uuid import uuid4

import pytest

from recap.commands.context import build_local_command_context
from recap.commands.errors import CommandValidationError
from recap.commands.service import CommandService
from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.db.namespace import Namespace
from recap.db.process import ProcessRun, ProcessTemplate
from recap.db.resource import Resource, ResourceTemplate, ResourceType
from recap.db.step import StepTemplate
from recap.lifecycle import LifecycleStatus


def _namespace(db_session, name="lifecycle"):
    namespace = Namespace(path=f"/{name}", status=LifecycleStatus.ACTIVE)
    db_session.add(namespace)
    db_session.flush()
    return namespace


@pytest.mark.parametrize("model", [ProcessTemplate, ResourceTemplate, Resource])
def test_aggregate_lifecycle_operations(model, db_session):
    namespace = _namespace(db_session, model.__name__.lower())
    kwargs = {"name": model.__name__, "namespace": namespace}
    if model in {ProcessTemplate, ResourceTemplate}:
        kwargs["version"] = "1"
    aggregate = model(**kwargs)
    db_session.add(aggregate)
    db_session.flush()

    aggregate.activate()
    assert aggregate.status is LifecycleStatus.ACTIVE
    aggregate.archive()
    assert aggregate.status is LifecycleStatus.ARCHIVED
    with pytest.raises(ValueError, match="Invalid lifecycle transition"):
        aggregate.activate()


def test_finalize_process_run_activates_template_and_bumps_revision(db_session):
    namespace = _namespace(db_session, "process-run")
    template = ProcessTemplate(name="process", version="1", namespace=namespace)
    template.step_templates["step"] = StepTemplate(
        name="step",
        attribute_group_templates=[
            AttributeGroupTemplate(
                name="params",
                attribute_templates=[
                    AttributeTemplate(name="value", value_type="int", default_value="1")
                ],
            )
        ],
    )
    db_session.add(template)
    db_session.flush()
    assert template.status is LifecycleStatus.MUTABLE
    assert template.revision == 1

    run = ProcessRun(
        name="run",
        description="description",
        template=template,
        namespace=namespace,
    )
    db_session.add(run)
    db_session.flush()

    assert template.status is LifecycleStatus.MUTABLE
    assert template.revision == 1
    assert run.status is LifecycleStatus.MUTABLE
    assert run.revision == 1

    run.finalize()
    db_session.flush()

    assert template.status is LifecycleStatus.ACTIVE
    assert template.revision == 2
    assert run.status is LifecycleStatus.ACTIVE
    assert run.revision == 2

    value = run.steps["step"].parameters["params"]._values["value"]
    value.value = 2
    with pytest.raises(ValueError, match="finalized process run"):
        db_session.flush()


def test_finalize_process_run_activates_assigned_resource_root(db_session):
    from recap.db.process import ResourceAssignment, ResourceSlot
    from recap.utils.general import Direction

    namespace = _namespace(db_session, "assignment")
    resource_type = ResourceType(name="assignment-sample")
    process_template = ProcessTemplate(name="process", version="1", namespace=namespace)
    slot = ResourceSlot(
        name="input",
        process_template=process_template,
        resource_type=resource_type,
        direction=Direction.input,
    )
    resource_template = ResourceTemplate(
        name="resource", version="1", namespace=namespace, types=[resource_type]
    )
    root = Resource(name="root", template=resource_template, namespace=namespace)
    child = Resource(
        name="child", template=resource_template, namespace=namespace, parent=root
    )
    run = ProcessRun(
        name="run",
        description="description",
        template=process_template,
        namespace=namespace,
    )
    db_session.add_all([slot, resource_template, root, run])
    db_session.flush()

    assignment = ResourceAssignment(process_run=run, resource_slot=slot, resource=child)
    db_session.add(assignment)
    db_session.flush()

    assert root.status is LifecycleStatus.MUTABLE
    assert root.revision == 1
    assert child.status is LifecycleStatus.MUTABLE
    assert child.revision == 1

    run.finalize()
    db_session.flush()

    assert root.status is LifecycleStatus.ACTIVE
    assert root.revision == 2
    assert child.status is LifecycleStatus.MUTABLE
    assert child.revision == 1


def test_process_run_rejects_illegal_finalize_transition(db_session):
    namespace = _namespace(db_session, "archived-run")
    template = ProcessTemplate(name="process", version="1", namespace=namespace)
    run = ProcessRun(
        name="run",
        description="description",
        template=template,
        namespace=namespace,
    )
    db_session.add_all([template, run])
    db_session.flush()
    run.archive()

    with pytest.raises(ValueError, match="Invalid lifecycle transition"):
        run.finalize()


@pytest.mark.parametrize(
    "options",
    [
        {"object_type": "resource_template", "recursive_depth": 1},
        {"object_type": "process_run", "recursive_depth": None},
        {"object_type": "resource", "status": "ARCHIVED", "recursive_depth": 1},
        {"object_type": "resource", "recursive_depth": -1},
    ],
)
def test_direct_lifecycle_service_validates_recursive_options(options):
    kwargs = {
        "object_type": "resource",
        "status": "ACTIVE",
        "object_id": uuid4(),
        "expected_revision": 1,
        **options,
    }
    with pytest.raises(CommandValidationError):
        CommandService(None).set_lifecycle_status(
            build_local_command_context(), **kwargs
        )


@pytest.mark.parametrize("target", ["root", "child"])
def test_postgres_finalization_locks_shared_aggregate_before_writes(
    client, monkeypatch, target
):
    """Exercise emitted lock queries on SQLite, without claiming PG concurrency."""
    from sqlalchemy import event
    from sqlalchemy.orm import Session, sessionmaker

    with client.build_resource_template(name="Lock node", type_names=["container"]):
        pass
    root = client.create_resource("lock-root", "Lock node")
    child = client.create_resource("lock-child", "Lock node", parent=root)
    client.create_resource("lock-grandchild", "Lock node", parent=child)
    engine = client.connection_state.engine
    operations = []

    def record(connection, cursor, sql, parameters, context, executemany):
        statement = context.compiled.statement if context.compiled is not None else None
        if (
            statement is not None
            and statement.is_select
            and statement._for_update_arg is not None
        ):
            operations.append(("lock", statement))
        elif (
            statement is not None
            and statement.is_update
            and statement.table.name == "resource"
        ):
            operations.append(("write", statement))

    # The engine retains its SQLite compiler and real isolated database. Only
    # the service's PostgreSQL-specific lock-selection branch is selected.
    monkeypatch.setattr(engine.dialect, "name", "postgresql")
    event.listen(engine, "before_cursor_execute", record)
    try:
        resource = root if target == "root" else child
        result = CommandService(sessionmaker(bind=engine)).set_lifecycle_status(
            build_local_command_context(),
            object_type="resource",
            object_id=resource.id,
            expected_revision=1,
            status="ACTIVE",
            recursive_depth=None,
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert result.status is LifecycleStatus.ACTIVE
    assert operations[0][0] == "lock"
    with Session(engine) as session:
        assert session.scalars(operations[0][1]).all() == [root.id]
