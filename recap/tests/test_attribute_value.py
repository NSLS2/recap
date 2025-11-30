import pytest

from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate, AttributeValue
from recap.db.resource import Resource, ResourceTemplate


def test_attribute_value_coercion_and_exclusive_field(db_session):
    tmpl = ResourceTemplate(name="Machine")
    group = AttributeGroupTemplate(name="Specs", resource_template=tmpl)
    attr = AttributeTemplate(
        name="Voltage",
        value_type="int",
        default_value="10",
        attribute_group_template=group,
    )
    db_session.add_all([tmpl, group, attr])
    db_session.flush()

    res = Resource(name="R1", template=tmpl)
    db_session.add(res)
    db_session.flush()

    av = res.properties["Specs"]._values["Voltage"]
    assert av.int_value == 10
    assert av.value == 10

    with pytest.raises(ValueError):
        av.bool_value = True  # wrong field for int type


def test_attribute_value_requires_target_owner(db_session):
    group = AttributeGroupTemplate(name="Specs")
    attr = AttributeTemplate(
        name="Orphaned",
        value_type="int",
        default_value=1,
        attribute_group_template=group,
    )
    db_session.add_all([group, attr])
    db_session.flush()

    with pytest.raises(ValueError):
        AttributeValue(template=attr)  # neither parameter nor property set


def test_attribute_value_unsupported_type_prevents_resource_init(db_session):
    tmpl = ResourceTemplate(name="Broken")
    group = AttributeGroupTemplate(name="Bad", resource_template=tmpl)
    AttributeTemplate(
        name="BadAttr",
        value_type="unsupported",
        default_value="x",
        attribute_group_template=group,
    )
    db_session.add_all([tmpl, group])
    db_session.flush()

    with pytest.raises(ValueError):
        Resource(name="BrokenRes", template=tmpl)
