def test_attribute(db_session):
    from recap.models.attribute import Attribute
    from recap.models.container import ContainerType

    prop_type = Attribute(name="TestProp", value_type="int", unit="kg")
    db_session.add(prop_type)

    container_type = ContainerType(name="TestContainer", attributes=[prop_type])
    db_session.add(container_type)

    db_session.commit()

    result = db_session.query(ContainerType).filter_by(name="TestContainer").first()

    assert result.attributes[0].unit == "kg"


def test_container_type(db_session):
    from recap.models.container import ContainerType

    container_type = ContainerType(name="test")
    db_session.add(container_type)
    db_session.commit()

    result = db_session.query(ContainerType).filter_by(name="test").first()
    assert result.name == "test"


def test_container(db_session):
    from recap.models.attribute import Attribute
    from recap.models.container import ContainerType, Container

    prop_type = Attribute(name="TestPropType", value_type="int", unit="kg", default_value="10")
    db_session.add(prop_type)
    container_type = ContainerType(name="TestContainerType", attributes=[prop_type])
    db_session.add(container_type)
    db_session.commit()

    container = Container(name="TestContainer", container_type=container_type)
    db_session.add(container)

    db_session.commit()

    result = db_session.query(Container).filter_by(name="TestContainer").first()

    assert result.properties[0].value == 10

    child_prop_type = Attribute(name="ChildPropTest", value_type="float", unit="mm", default_value="2.2")
    db_session.add(child_prop_type)

    child_container_type = ContainerType(name="ChildTestContainerType", attributes=[child_prop_type])
    db_session.add(child_container_type)
    db_session.commit()

    child_container_a1 = Container(name="A1", container_type=child_container_type)
    child_container_a2 = Container(name="A2", container_type=child_container_type)

    container.children.append(child_container_a1)
    container.children.append(child_container_a2)
    db_session.commit()

    result = db_session.query(Container).filter_by(name="TestContainer").first()

    assert len(result.children) == 2
    assert result.children[0].name == "A1"
    assert result.children[1].properties[0].value == 2.2
