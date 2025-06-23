def test_container(db_session):
    from recap.models.attribute import Attribute
    from recap.models.step import StepTemplate
    from recap.models.resource import Resource, ResourceTemplate, ResourceType
    from recap.models.process import ProcessTemplate, ResourceSlot, ProcessRun

    param_type = Attribute(
        name="TestParamType", value_type="float", unit="uL", default_value="4.0"
    )
    db_session.add(param_type)
    process_template = ProcessTemplate(name="TestProcessTemplate")
    container_type = ResourceType(name="container")
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
    process_template.resource_slots.append(container_1_resource_slot)
    process_template.resource_slots.append(container_2_resource_slot)
    step_template = StepTemplate(
        name="TestActionType",
        attributes=[param_type],
        process_template=process_template,
        resource_slots=[
            (container_1_resource_slot, "source_container"),
            (container_2_resource_slot, "dest_container"),
        ],
    )

    db_session.add(step_template)
    db_session.commit()

    child_prop_type = Attribute(
        name="ChildPropTest", value_type="float", unit="mm", default_value="2.2"
    )
    db_session.add(child_prop_type)
    child_container_template = ResourceTemplate(
        name="ChildTestContainerType",
        ref_name="ctc",
        type=container_type,
        attributes=[child_prop_type],
    )
    db_session.add(child_container_template)
    db_session.commit()

    child_container_a1 = Resource(name="A1", template=child_container_template)
    child_container_a2 = Resource(name="A2", template=child_container_template)
    process_run = ProcessRun(
        name="Test Process Run",
        description="This is a test",
        template=process_template,
        resources=[
            (child_container_a1, container_1_resource_slot),
            (child_container_a2, container_2_resource_slot),
        ],
    )
    db_session.add(process_run)
    db_session.commit()

    result: ProcessRun = (
        db_session.query(ProcessRun).filter_by(name="Test Process Run").first()
    )

    assert any(r.name == "A1" for r in result.resources)
    assert result.steps[0].parameters[0].value == 4.0
