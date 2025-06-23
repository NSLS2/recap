def test_attribute(db_session):
    from recap.models.attribute import Attribute
    from recap.models.resource import ResourceTemplate, ResourceType

    prop_type = Attribute(name="TestProp", value_type="int", unit="kg")
    db_session.add(prop_type)

    container_type = ResourceType(name="container")
    container_template = ResourceTemplate(
        name="TestContainer",
        ref_name="tct",
        attributes=[prop_type],
        type=container_type,
    )
    db_session.add(container_type)
    db_session.add(container_template)

    db_session.commit()

    result = db_session.query(ResourceTemplate).filter_by(name="TestContainer").first()

    assert result.attributes[0].unit == "kg"
    assert result.type.name == "container"


def test_container_type(db_session):
    from recap.models.resource import ResourceTemplate, ResourceType

    container_type = ResourceType(name="container")
    container_type = ResourceTemplate(name="test", ref_name="test", type=container_type)
    db_session.add(container_type)
    db_session.commit()

    result = db_session.query(ResourceTemplate).filter_by(name="test").first()
    assert result.name == "test"


def test_container(db_session):
    from recap.models.attribute import Attribute
    from recap.models.resource import ResourceTemplate, Resource, ResourceType

    prop_type = Attribute(
        name="TestPropType", value_type="int", unit="kg", default_value="10"
    )
    db_session.add(prop_type)
    container_type = ResourceType(name="container")
    container_template = ResourceTemplate(
        name="TestContainerType",
        ref_name="test",
        attributes=[prop_type],
        type=container_type,
    )
    db_session.add(container_type)
    db_session.commit()

    container = Resource(
        name="TestContainer", ref_name="blah", template=container_template
    )
    db_session.add(container)

    db_session.commit()

    result = db_session.query(Resource).filter_by(name="TestContainer").first()

    assert result.properties[0].value == 10

    child_prop_type = Attribute(
        name="ChildPropTest", value_type="float", unit="mm", default_value="2.2"
    )
    db_session.add(child_prop_type)

    child_container_type = ResourceTemplate(
        name="ChildTestContainerType",
        ref_name="ctct",
        attributes=[child_prop_type],
        type=container_type,
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
    assert result.children[1].properties[0].value == 2.2
