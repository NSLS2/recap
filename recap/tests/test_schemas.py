from datetime import UTC, datetime
from uuid import uuid4

import pytest

from recap.schemas.attribute import (
    AttributeEnumOptionSchema,
    AttributeGroupTemplateSchema,
    AttributeTemplateSchema,
    AttributeTemplateValidator,
)
from recap.schemas.common import Attribute, ValueType
from recap.schemas.resource import PropertySchema
from recap.schemas.step import ParameterSchema


def _now():
    return datetime.now(UTC)


def _attribute_template_schema(
    name: str,
    value_type: str,
    default_value,
    unit: str | None = None,
    options: list[AttributeEnumOptionSchema] | None = None,
):
    stamp = _now()
    return AttributeTemplateSchema(
        id=uuid4(),
        create_date=stamp,
        modified_date=stamp,
        name=name,
        slug=name.lower(),
        value_type=value_type,
        unit=unit,
        default_value=default_value,
        enum_options=options or [],
    )


def _attribute_group_schema(name: str, templates: list[AttributeTemplateSchema]):
    stamp = _now()
    return AttributeGroupTemplateSchema(
        id=uuid4(),
        create_date=stamp,
        modified_date=stamp,
        name=name,
        slug=name.lower(),
        attribute_templates=templates,
    )


def _enum_option_schema(
    value: str, label: str | None = None, payload: dict | None = None
):
    stamp = _now()
    return AttributeEnumOptionSchema(
        id=uuid4(),
        create_date=stamp,
        modified_date=stamp,
        value=value,
        label=label,
        payload=payload,
    )


def test_attribute_schema_validates_default_value_type():
    Attribute(
        name="count",
        slug="count",
        value_type=ValueType.INT,
        default_value=1,
    )
    with pytest.raises(ValueError):
        Attribute(
            name="count",
            slug="count",
            value_type=ValueType.INT,
            default_value="ten",
        )


def test_attribute_template_validator_coerces_and_rejects_defaults():
    validator = AttributeTemplateValidator(name="Voltage", type="int", default="7")
    assert validator.default == 7

    with pytest.raises(ValueError):
        AttributeTemplateValidator(name="When", type="datetime", default="not-a-date")


def test_attribute_template_validator_enforces_enum_options():
    validator = AttributeTemplateValidator(
        name="drop_position", type="enum", options=["c", "ul"], default="ul"
    )
    assert validator.default == "ul"

    with pytest.raises(ValueError):
        AttributeTemplateValidator(
            name="drop_position", type="enum", options=["c"], default="bogus"
        )


def test_parameter_schema_coerces_values_and_rejects_unknown():
    voltage = _attribute_template_schema("Voltage", "int", 5)
    enabled = _attribute_template_schema("Enabled", "bool", False)
    group_schema = _attribute_group_schema("Inputs", [voltage, enabled])
    stamp = _now()
    schema = ParameterSchema(
        id=uuid4(),
        create_date=stamp,
        modified_date=stamp,
        template=group_schema,
        values={"Voltage": "10", "Enabled": "true"},
    )

    assert schema.values.voltage == 10
    assert schema.values.enabled is True

    with pytest.raises(ValueError):
        ParameterSchema(
            id=uuid4(),
            create_date=stamp,
            modified_date=stamp,
            template=group_schema,
            values={"Voltage": "5", "Unknown": 1},
        )


def test_parameter_schema_validates_enum_option_membership():
    center = _enum_option_schema("c")
    upper_left = _enum_option_schema("ul")
    drop_pos = _attribute_template_schema(
        "drop_position", "enum", "c", options=[center, upper_left]
    )
    group_schema = _attribute_group_schema("Positions", [drop_pos])
    stamp = _now()

    schema = ParameterSchema(
        id=uuid4(),
        create_date=stamp,
        modified_date=stamp,
        template=group_schema,
        values={"drop_position": "ul"},
    )
    assert schema.values.drop_position == "ul"

    with pytest.raises(ValueError):
        ParameterSchema(
            id=uuid4(),
            create_date=stamp,
            modified_date=stamp,
            template=group_schema,
            values={"drop_position": "bogus"},
        )


def test_parameter_schema_exposes_typed_values_model():
    voltage = _attribute_template_schema("Voltage", "int", 5)
    enabled = _attribute_template_schema("Enabled", "bool", False)
    group_schema = _attribute_group_schema("Inputs", [voltage, enabled])
    stamp = _now()

    schema = ParameterSchema(
        id=uuid4(),
        create_date=stamp,
        modified_date=stamp,
        template=group_schema,
        values={"Voltage": "10", "Enabled": "true"},
    )

    assert schema.values.voltage == 10
    assert schema.values.enabled is True
    assert schema.values.model_dump(by_alias=True) == {"Voltage": 10, "Enabled": True}


def test_property_schema_value_coercion_matches_template():
    temperature = _attribute_template_schema("Temp", "float", 37.5)
    group_schema = _attribute_group_schema("Environment", [temperature])
    stamp = _now()
    property_schema = PropertySchema(
        id=uuid4(),
        create_date=stamp,
        modified_date=stamp,
        template=group_schema,
        values={"Temp": "25.5"},
    )

    assert property_schema.values.temp == pytest.approx(25.5)


def test_parameter_schema_rejects_uncoercible_values():
    duration = _attribute_template_schema("Duration", "int", 0)
    group_schema = _attribute_group_schema("Timing", [duration])
    stamp = _now()
    with pytest.raises(ValueError):
        ParameterSchema(
            id=uuid4(),
            create_date=stamp,
            modified_date=stamp,
            template=group_schema,
            values={"Duration": "not-an-int"},
        )
