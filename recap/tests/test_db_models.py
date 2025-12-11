import pytest
from sqlalchemy.exc import IntegrityError

from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.db.campaign import Campaign
from recap.db.process import (
    Direction,
    ProcessRun,
    ProcessTemplate,
    ResourceAssignment,
    ResourceSlot,
)
from recap.db.resource import Resource, ResourceTemplate, ResourceType
from recap.db.step import StepTemplate, StepTemplateResourceSlotBinding
from recap.utils.general import make_slug


def test_attribute_group_template_slug_and_constraint(db_session):
    process_template = ProcessTemplate(name="Pipeline", version="v1")
    step_template = StepTemplate(name="Prep", process_template=process_template)
    resource_template = ResourceTemplate(name="Instrument")
    valid_group = AttributeGroupTemplate(
        name="Group One", resource_template=resource_template
    )

    db_session.add_all(
        [process_template, step_template, resource_template, valid_group]
    )
    db_session.flush()

    assert valid_group.slug == make_slug("Group One")

    invalid_group = AttributeGroupTemplate(
        name="Invalid",
        resource_template=resource_template,
        step_template=step_template,
    )
    db_session.add(invalid_group)

    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_resource_initialization_from_template(db_session):
    resource_template = ResourceTemplate(name="Parent")
    attr_group = AttributeGroupTemplate(
        name="Settings", resource_template=resource_template
    )
    AttributeTemplate(
        name="Voltage",
        value_type="int",
        default_value="5",
        attribute_group_template=attr_group,
    )
    child_template = ResourceTemplate(name="Child", parent=resource_template)
    db_session.add_all([resource_template, attr_group, child_template])
    db_session.flush()

    resource = Resource(name="Main Resource", template=resource_template)
    db_session.add(resource)
    db_session.flush()

    assert set(resource.properties.keys()) == {"Settings"}
    settings = resource.properties["Settings"]
    assert settings.values["Voltage"] == 5
    assert len({id(child) for child in resource.children.values()}) == 1
    child = next(iter(resource.children.values()))
    assert child.template is child_template
    assert child.name == child_template.name


def test_process_run_validates_resource_assignments(db_session):
    resource_type = ResourceType(name="Microscope")
    process_template = ProcessTemplate(name="Acquisition", version="v1")
    slot = ResourceSlot(
        name="instrument",
        process_template=process_template,
        resource_type=resource_type,
        direction=Direction.input,
    )
    campaign = Campaign(name="Campaign", proposal="P1", saf=None, meta_data=None)

    matching_template = ResourceTemplate(
        name="MicroscopeTemplate", types=[resource_type]
    )
    matching_resource = Resource(name="Scope A", template=matching_template)

    wrong_type = ResourceType(name="Other")
    wrong_template = ResourceTemplate(name="OtherTemplate", types=[wrong_type])
    wrong_resource = Resource(name="Other A", template=wrong_template)

    run = ProcessRun(
        name="run-1",
        description="desc",
        template=process_template,
        campaign=campaign,
    )

    db_session.add_all(
        [
            resource_type,
            process_template,
            slot,
            campaign,
            matching_template,
            matching_resource,
            wrong_type,
            wrong_template,
            wrong_resource,
            run,
        ]
    )
    db_session.flush()

    run.resources[slot] = matching_resource
    db_session.flush()
    assert run.assignments[slot].resource is matching_resource

    slot_two = ResourceSlot(
        name="instrument-2",
        process_template=process_template,
        resource_type=resource_type,
        direction=Direction.input,
    )
    db_session.add(slot_two)
    with pytest.raises(ValueError):
        run.resources[slot_two] = wrong_resource

    another_resource = Resource(name="Scope B", template=matching_template)
    db_session.add(another_resource)
    with pytest.raises(ValueError):
        run.assignments[slot] = ResourceAssignment(
            resource_slot=slot, resource=another_resource
        )


def test_step_initializes_parameters_from_template(db_session):
    process_template = ProcessTemplate(name="Prep", version="1.0")
    step_template = StepTemplate(name="Incubate", process_template=process_template)
    attr_group = AttributeGroupTemplate(name="Conditions", step_template=step_template)
    AttributeTemplate(
        name="Duration",
        value_type="int",
        default_value=30,
        attribute_group_template=attr_group,
    )
    campaign = Campaign(name="C1", proposal="P1", saf=None, meta_data=None)

    run = ProcessRun(
        name="run-001",
        description="desc",
        template=process_template,
        campaign=campaign,
    )
    db_session.add_all([process_template, step_template, attr_group, campaign, run])
    db_session.flush()

    (step,) = run.steps
    assert step.name == step_template.name
    assert set(step.parameters.keys()) == {"Conditions"}
    param = step.parameters["Conditions"]
    assert param.values["Duration"] == 30


def test_resource_slug_and_active_toggle(db_session):
    tmpl1 = ResourceTemplate(name="Machine")
    tmpl2 = ResourceTemplate(name="Machine2")
    db_session.add_all([tmpl1, tmpl2])
    db_session.flush()

    first = Resource(name="Shared Name", template=tmpl1)
    db_session.add(first)
    db_session.flush()

    assert first.slug == make_slug("Shared Name")
    assert first.active is True

    second = Resource(name="Shared Name", template=tmpl2)
    db_session.add(second)
    db_session.flush()

    # new one is active, old one was deactivated by the before_insert hook
    assert second.active is True
    db_session.refresh(first)
    assert first.active is False


def test_resource_children_respect_max_depth(db_session):
    tmpl = ResourceTemplate(name="Parent")
    ResourceTemplate(name="Child", parent=tmpl)
    db_session.add(tmpl)
    db_session.flush()

    resource = Resource(name="Root", template=tmpl, _max_depth=0)
    db_session.add(resource)
    db_session.flush()

    assert resource.children == {}


def test_property_values_reject_unknown_keys(db_session):
    tmpl = ResourceTemplate(name="Machine")
    group = AttributeGroupTemplate(name="Props", resource_template=tmpl)
    AttributeTemplate(
        name="Voltage",
        value_type="int",
        default_value=5,
        attribute_group_template=group,
    )
    db_session.add_all([tmpl, group])
    db_session.flush()

    res = Resource(name="Machine A", template=tmpl)
    db_session.add(res)
    db_session.flush()

    with pytest.raises(KeyError):
        res.properties["Props"].values["Unknown"] = 1


def test_resource_slot_name_unique_per_process_template(db_session):
    pt = ProcessTemplate(name="PT", version="1")
    slot1 = ResourceSlot(
        name="instrument",
        process_template=pt,
        resource_type=ResourceType(name="t"),
        direction=Direction.input,
    )
    slot2 = ResourceSlot(
        name="instrument",
        process_template=pt,
        resource_type=ResourceType(name="t2"),
        direction=Direction.output,
    )
    db_session.add_all([pt, slot1, slot2])
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_step_uses_template_name_when_missing(db_session):
    pt = ProcessTemplate(name="PT2", version="1")
    st = StepTemplate(name="T1", process_template=pt)
    campaign = Campaign(name="C2", proposal="p", saf=None, meta_data=None)
    run = ProcessRun(name="run-step", description="", template=pt, campaign=campaign)
    db_session.add_all([pt, st, campaign, run])
    db_session.flush()

    # ProcessRun __init__ auto-creates steps; ensure the default naming uses the template
    (auto_step,) = run.steps
    assert auto_step.name == st.name


def test_step_template_binding_role_unique(db_session):
    pt = ProcessTemplate(name="PT3", version="1")
    st = StepTemplate(name="T2", process_template=pt)
    rt = ResourceType(name="slot-type")
    slot = ResourceSlot(
        name="slot",
        process_template=pt,
        resource_type=rt,
        direction=Direction.input,
    )
    db_session.add_all([pt, st, rt, slot])
    db_session.flush()

    binding1 = StepTemplateResourceSlotBinding(
        step_template_id=st.id, resource_slot_id=slot.id, role="primary"
    )
    binding2 = StepTemplateResourceSlotBinding(
        step_template_id=st.id, resource_slot_id=slot.id, role="primary"
    )
    db_session.add_all([binding1, binding2])

    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()
