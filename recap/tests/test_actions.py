from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.db.campaign import Campaign
from recap.utils.database import get_or_create


def test_container(db_session):
    from recap.db.process import ProcessRun, ProcessTemplate, ResourceSlot
    from recap.db.resource import Resource, ResourceTemplate, ResourceType
    from recap.db.step import StepTemplate

    param_type = AttributeGroupTemplate(
        name="TestParamType",
    )
    param_value_template = AttributeTemplate(
        name="volume", value_type="float", unit="uL", default_value="4.0"
    )
    param_type.attribute_templates.append(param_value_template)
    db_session.add(param_type)
    db_session.commit()
    process_template = ProcessTemplate(name="TestProcessTemplate", version="1.0")
    db_session.add(process_template)
    # container_type = ResourceType(name="container")
    container_type, _ = get_or_create(
        db_session, ResourceType, where={"name": "container"}
    )
    container_1_resource_slot = ResourceSlot(
        process_template=process_template,
        resource_type=container_type,
        name="container1",
        direction="input",
    )
    container_2_resource_slot = ResourceSlot(
        process_template=process_template,
        resource_type=container_type,
        name="container2",
        direction="input",
    )
    db_session.add(container_1_resource_slot)
    db_session.add(container_2_resource_slot)
    process_template.resource_slots.append(container_1_resource_slot)
    process_template.resource_slots.append(container_2_resource_slot)
    step_template = StepTemplate(
        name="TestActionType",
        attribute_group_templates=[param_type],
        process_template=process_template,
    )
    step_template.resource_slots["source_container"] = container_1_resource_slot
    step_template.resource_slots["dest_container"] = container_2_resource_slot

    db_session.add(step_template)
    db_session.commit()

    child_prop_type = AttributeTemplate(
        name="ChildPropTest", value_type="float", unit="mm", default_value="2.2"
    )
    child_attr_template = AttributeGroupTemplate(name="Child test")
    child_attr_template.attribute_templates.append(child_prop_type)
    db_session.add(child_prop_type)
    child_container_template = ResourceTemplate(
        name="ChildTestContainerType",
        types=[container_type],
        attribute_group_templates=[child_attr_template],
    )
    db_session.add(child_container_template)
    db_session.commit()

    child_container_a1 = Resource(name="A1", template=child_container_template)
    child_container_a2 = Resource(name="A2", template=child_container_template)
    campaign = Campaign(name="Test campaign", proposal="123456")
    process_run = ProcessRun(
        name="Test Process Run",
        description="This is a test",
        template=process_template,
        campaign=campaign,
    )
    process_run.resources[container_1_resource_slot] = child_container_a1
    process_run.resources[container_2_resource_slot] = child_container_a2
    db_session.add(process_run)
    db_session.commit()

    result: ProcessRun = (
        db_session.query(ProcessRun).filter_by(name="Test Process Run").first()
    )

    assert result.resources[container_1_resource_slot].name == "A1"
    assert (
        result.steps["TestActionType"].parameters["TestParamType"].values["volume"]
        == 4.0
    )
