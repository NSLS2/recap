def test_container(db_session):
    from recap.models.attribute import Attribute
    from recap.models.actions import ActionType, Action
    from recap.models.container import Container, ContainerType

    param_type = Attribute(name="TestParamType", value_type="float", unit="uL", default_value="4.0")
    db_session.add(param_type)
    action_type = ActionType(name="TestActionType", attributes=[param_type])
    db_session.add(action_type)
    db_session.commit()

    child_prop_type = Attribute(name="ChildPropTest", value_type="float", unit="mm", default_value="2.2")
    db_session.add(child_prop_type)

    child_container_type = ContainerType(name="ChildTestContainerType", attributes=[child_prop_type])
    db_session.add(child_container_type)
    db_session.commit()

    child_container_a1 = Container(name="A1", container_type=child_container_type)
    child_container_a2 = Container(name="A2", container_type=child_container_type)
    db_session.commit()

    action = Action(
        name="TestAction",
        action_type=action_type,
        source_container=child_container_a1,
        dest_container=child_container_a2,
    )
    db_session.add(action)
    db_session.commit()

    result = db_session.query(Action).filter_by(name="TestAction").first()

    assert result.parameters[0].value == 4.0
