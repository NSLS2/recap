from recap.models.attribute import AttributeValueTemplate


def test_attribute(db_session):
    from recap.models.attribute import AttributeTemplate, AttributeValueTemplate
    from recap.models.resource import ResourceTemplate, ResourceType

    prop_type = AttributeTemplate(name="TestProp")  # , value_type="int", unit="kg")
    prop_value_template = AttributeValueTemplate(
        name="test_value", value_type="int", unit="kg", default_value=3
    )
    prop_type.value_templates.append(prop_value_template)
    db_session.add(prop_type)

    container_type = ResourceType(name="container")
    container_template = ResourceTemplate(
        name="TestContainer",
        attribute_templates=[prop_type],
        types=[container_type],
    )
    db_session.add(container_type)
    db_session.add(container_template)

    db_session.commit()

    result = db_session.query(ResourceTemplate).filter_by(name="TestContainer").first()

    assert result.attribute_templates[0].value_templates[0].unit == "kg"
    assert result.types[0].name == "container"


def test_container_type(db_session):
    from recap.models.resource import ResourceTemplate, ResourceType

    container_type = ResourceType(name="container")
    container = ResourceTemplate(name="test", types=[container_type])
    db_session.add(container_type)
    db_session.add(container)
    db_session.commit()

    result = db_session.query(ResourceTemplate).filter_by(name="test").first()
    assert result.name == "test"


def test_container(db_session):
    from recap.models.attribute import AttributeTemplate
    from recap.models.resource import Resource, ResourceTemplate, ResourceType

    prop_type = AttributeTemplate(name="TestPropType")  # ,
    prop_value_template = AttributeValueTemplate(
        name="test_prop_val", value_type="int", unit="kg", default_value="10"
    )
    prop_type.value_templates.append(prop_value_template)
    db_session.add(prop_type)
    container_type = ResourceType(name="container")
    container_template = ResourceTemplate(
        name="TestContainerType",
        attribute_templates=[prop_type],
        types=[container_type],
    )
    db_session.add(container_type)
    db_session.commit()

    container = Resource(name="TestContainer", template=container_template)
    db_session.add(container)

    db_session.commit()

    result = db_session.query(Resource).filter_by(name="TestContainer").first()

    assert result.properties["TestPropType"].values["test_prop_val"] == 10

    child_prop_type = AttributeTemplate(name="ChildPropTest")
    child_value_template = AttributeValueTemplate(
        name="child_prop_test", value_type="float", unit="mm", default_value="2.2"
    )
    child_prop_type.value_templates.append(child_value_template)
    db_session.add(child_prop_type)

    child_container_type = ResourceTemplate(
        name="ChildTestContainerType",
        attribute_templates=[child_prop_type],
        types=[container_type],
    )
    db_session.add(child_container_type)
    db_session.commit()

    child_container_a1 = Resource(name="A1", template=child_container_type)
    child_container_a2 = Resource(name="A2", template=child_container_type)

    container.children.append(child_container_a1)
    container.children.append(child_container_a2)
    db_session.commit()

    result = db_session.query(Resource).filter_by(name="TestContainer").first()

    assert len(result.children) == 2
    assert result.children[0].name == "A1"
    assert (
        result.children[1].properties["ChildPropTest"].values["child_prop_test"] == 2.2
    )
