import pytest

from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.db.campaign import Campaign
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


def test_first_process_run_activates_template_and_finalize_freezes_run(db_session):
    namespace = _namespace(db_session, "process-run")
    campaign = Campaign(
        id=namespace.id, name="campaign", proposal="p", saf=None, meta_data=None
    )
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
    db_session.add_all([campaign, template])
    db_session.flush()

    run = ProcessRun(
        name="run",
        description="description",
        template=template,
        campaign=campaign,
        namespace=namespace,
    )
    db_session.add(run)
    db_session.flush()

    assert template.status is LifecycleStatus.ACTIVE
    assert run.status is LifecycleStatus.MUTABLE
    run.finalize()
    db_session.flush()
    assert run.status is LifecycleStatus.ACTIVE

    value = run.steps["step"].parameters["params"]._values["value"]
    value.value = 2
    with pytest.raises(ValueError, match="finalized process run"):
        db_session.flush()


def test_assignment_activates_nested_resource_root_in_same_flush(db_session):
    from recap.db.process import ResourceAssignment, ResourceSlot
    from recap.utils.general import Direction

    namespace = _namespace(db_session, "assignment")
    campaign = Campaign(
        id=namespace.id, name="campaign", proposal="p", saf=None, meta_data=None
    )
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
        campaign=campaign,
        namespace=namespace,
    )
    db_session.add_all([campaign, slot, resource_template, root, run])
    db_session.flush()

    assignment = ResourceAssignment(process_run=run, resource_slot=slot, resource=child)
    db_session.add(assignment)
    db_session.flush()

    assert root.status is LifecycleStatus.ACTIVE
    assert child.status is LifecycleStatus.MUTABLE


def test_process_run_rejects_illegal_finalize_transition(db_session):
    namespace = _namespace(db_session, "archived-run")
    campaign = Campaign(
        id=namespace.id, name="campaign", proposal="p", saf=None, meta_data=None
    )
    template = ProcessTemplate(name="process", version="1", namespace=namespace)
    run = ProcessRun(
        name="run",
        description="description",
        template=template,
        campaign=campaign,
        namespace=namespace,
    )
    db_session.add_all([campaign, template, run])
    db_session.flush()
    run.archive()

    with pytest.raises(ValueError, match="Invalid lifecycle transition"):
        run.finalize()
