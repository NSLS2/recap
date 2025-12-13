import pytest

from recap.adapter.local import LocalBackend
from recap.db.attribute import (
    AttributeEnumOption,
    AttributeGroupTemplate,
    AttributeTemplate,
    AttributeValue,
)
from recap.db.resource import Resource, ResourceTemplate
from recap.schemas.resource import ResourceTemplateSchema


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
    tmpl = ResourceTemplate(name="SpecsTemplate")
    group = AttributeGroupTemplate(name="Specs", resource_template=tmpl)
    attr = AttributeTemplate(
        name="Orphaned",
        value_type="int",
        default_value=1,
        attribute_group_template=group,
    )
    db_session.add_all([tmpl, group, attr])
    db_session.flush()

    with pytest.raises(ValueError) as excinfo:
        AttributeValue(template=attr)  # neither parameter nor property set
    assert "Parameter or Property must be set" in str(excinfo.value)


def test_enum_attribute_value_maps_options_and_payload(db_session):
    tmpl = ResourceTemplate(name="Dropper")
    group = AttributeGroupTemplate(name="Positions", resource_template=tmpl)
    attr = AttributeTemplate(
        name="drop_position",
        value_type="enum",
        default_value="c",
        attribute_group_template=group,
        enum_options=[
            AttributeEnumOption(value="c", payload={"coords": [0.5, 0.5]}),
            AttributeEnumOption(value="ul", payload={"coords": [0.0, 1.0]}),
        ],
    )
    db_session.add_all([tmpl, group, attr])
    db_session.flush()

    res = Resource(name="R1", template=tmpl)
    db_session.add(res)
    db_session.flush()

    av = res.properties["Positions"]._values["drop_position"]
    assert av.value == "c"
    assert av.enum_payload == {"coords": [0.5, 0.5]}
    assert av.enum_option_id is not None

    av.set_value("ul")
    assert av.value == "ul"
    assert av.enum_payload == {"coords": [0.0, 1.0]}

    with pytest.raises(ValueError):
        av.set_value("bogus")


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


def test_add_attr_group_reuses_existing_group(db_session):
    tmpl = ResourceTemplate(name="RT")
    db_session.add(tmpl)
    db_session.flush()

    backend = LocalBackend(lambda: db_session)
    # Manually bind the existing session for this test
    backend._session = db_session
    ref = ResourceTemplateSchema.model_validate(tmpl)

    first = backend.add_attr_group("content", ref)
    second = backend.add_attr_group("content", ref)

    assert first.id == second.id
