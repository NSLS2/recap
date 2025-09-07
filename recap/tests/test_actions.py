from recap.models.attribute import AttributeTemplate, AttributeValueTemplate


def test_container(db_session):
    from recap.models.step import StepTemplate
    from recap.models.resource import Resource, ResourceTemplate, ResourceType
    from recap.models.process import ProcessTemplate, ResourceSlot, ProcessRun

    param_type = AttributeTemplate(
        name="TestParamType",
    )
    param_value_template = AttributeValueTemplate(
        name="volume", value_type="float", unit="uL", default_value="4.0"
    )
    param_type.value_templates.append(param_value_template)
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
        attribute_templates=[param_type],
        process_template=process_template,
        resource_slots=[
            (container_1_resource_slot, "source_container"),
            (container_2_resource_slot, "dest_container"),
        ],
    )

    db_session.add(step_template)
    db_session.commit()

    child_prop_type = AttributeValueTemplate(
        name="ChildPropTest", value_type="float", unit="mm", default_value="2.2"
    )
    child_attr_template = AttributeTemplate(name="Child test")
    child_attr_template.value_templates.append(child_prop_type)
    db_session.add(child_prop_type)
    child_container_template = ResourceTemplate(
        name="ChildTestContainerType",
        ref_name="ctc",
        type=container_type,
        attribute_templates=[child_attr_template],
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

    result: ProcessRun = db_session.query(ProcessRun).filter_by(name="Test Process Run").first()

    assert any(r.name == "A1" for r in result.resources)
    assert result.steps[0].parameters["TestParamType"].values["volume"] == 4.0
