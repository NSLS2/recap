import pytest

from recap.adapter.local import LocalBackend
from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate, AttributeValue
from recap.db.resource import Resource, ResourceTemplate
from recap.schemas.resource import ResourceTemplateSchema


def test_attribute_value_coercion_and_json_storage(db_session):
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
    assert av.value == 10
    assert av.value_json == 10
    assert av.metadata_json == {}

    av.value = "12"
    db_session.flush()
    assert av.value == 12
    assert av.value_json == 12


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


def test_attribute_value_serializes_datetime_to_iso(db_session):
    tmpl = ResourceTemplate(name="Timey")
    group = AttributeGroupTemplate(name="Props", resource_template=tmpl)
    attr = AttributeTemplate(
        name="When",
        value_type="datetime",
        default_value="2024-01-01T00:00:00Z",
        attribute_group_template=group,
    )
    db_session.add_all([tmpl, group, attr])
    db_session.flush()

    res = Resource(name="Timed", template=tmpl)
    db_session.add(res)
    db_session.flush()

    av = res.properties["Props"]._values["When"]
    assert isinstance(av.value_json, str)
    assert av.value_json.startswith("2024-01-01T00:00:00")
    assert av.value.year == 2024


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
