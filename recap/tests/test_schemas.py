from datetime import UTC, datetime
from uuid import uuid4

import pytest

from recap.schemas.attribute import (
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
    metadata: dict | None = None,
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
        metadata=metadata or {},
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

    assert schema.values.voltage.value == 10
    assert schema.values.enabled.value is True

    with pytest.raises(ValueError):
        ParameterSchema(
            id=uuid4(),
            create_date=stamp,
            modified_date=stamp,
            template=group_schema,
            values={"Voltage": "5", "Unknown": 1},
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

    assert schema.values.voltage.value == 10
    assert schema.values.enabled.value is True
    assert schema.values.model_dump(by_alias=True) == {
        "Voltage": {"value": 10, "unit": None},
        "Enabled": {"value": True, "unit": None},
    }


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

    assert property_schema.values.temp.value == pytest.approx(25.5)


def test_property_schema_shortcut_attribute_access():
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

    # Shortcut: .temp instead of .values.temp
    assert property_schema.temp is property_schema.values.temp
    assert property_schema.temp.value == pytest.approx(25.5)

    # Shortcut setter: scalar assignment mutates .value in-place (unit preserved)
    property_schema.temp = 99.0
    assert property_schema.values.temp.value == pytest.approx(99.0)
    assert property_schema.temp.value == pytest.approx(99.0)

    # Unit is set directly on the AttributeValueSchema
    property_schema.temp.unit = "degC"
    assert property_schema.temp.unit == "degC"

    # Scalar re-assignment preserves the unit that was just set
    property_schema.temp = 37.0
    assert property_schema.temp.value == pytest.approx(37.0)
    assert property_schema.temp.unit == "degC"  # unit unchanged

    # __str__ returns "<value><unit>"
    assert str(property_schema.temp) == "37.0degC"

    # Unknown names should still raise AttributeError
    with pytest.raises(AttributeError):
        _ = property_schema.nonexistent_field

    # Real PropertySchema fields are unaffected
    assert property_schema.values is not None
    assert property_schema.template is group_schema


def test_parameter_schema_shortcut_attribute_access():
    voltage = _attribute_template_schema("Voltage", "int", 5)
    enabled = _attribute_template_schema("Enabled", "bool", False)
    group_schema = _attribute_group_schema("Inputs", [voltage, enabled])
    stamp = _now()
    from recap.schemas.step import ParameterSchema as PS

    schema = PS(
        id=uuid4(),
        create_date=stamp,
        modified_date=stamp,
        template=group_schema,
        values={"Voltage": "10", "Enabled": "true"},
    )

    # Shortcut getter
    assert schema.voltage is schema.values.voltage
    assert schema.voltage.value == 10
    assert schema.enabled.value is True

    # Shortcut setter: scalar mutates .value in-place
    schema.voltage = 42
    assert schema.values.voltage.value == 42
    assert schema.voltage.value == 42

    # Unit is set directly on the AttributeValueSchema
    schema.voltage.unit = "mV"
    assert schema.voltage.unit == "mV"

    # Re-assignment preserves the unit
    schema.voltage = 7
    assert schema.voltage.value == 7
    assert schema.voltage.unit == "mV"  # unit unchanged

    # __str__ returns "<value><unit>"
    assert str(schema.voltage) == "7mV"

    # Unknown names raise AttributeError
    with pytest.raises(AttributeError):
        _ = schema.nonexistent_field

    # Real ParameterSchema fields are unaffected
    assert schema.values is not None
    assert schema.template is group_schema


def test_property_schema_bracket_assignment_preserves_unit():
    volume = _attribute_template_schema("Volume", "float", 10.0, unit="uL")
    group_schema = _attribute_group_schema("Content", [volume])
    stamp = _now()
    prop = PropertySchema(
        id=uuid4(),
        create_date=stamp,
        modified_date=stamp,
        template=group_schema,
        values={"Volume": 10.0},
    )

    # Override the unit
    prop.volume.unit = "mL"

    # Bracket assignment by slug — unit must be preserved
    prop.values["volume"] = 50.0
    assert prop.volume.value == pytest.approx(50.0)
    assert prop.volume.unit == "mL"

    # Bracket assignment by original name — same behaviour
    prop.values["Volume"] = 99.0
    assert prop.volume.value == pytest.approx(99.0)
    assert prop.volume.unit == "mL"


def test_parameter_schema_bracket_assignment_preserves_unit():
    volume = _attribute_template_schema("Volume", "float", 25.0, unit="nL")
    group_schema = _attribute_group_schema("Echo", [volume])
    stamp = _now()
    from recap.schemas.step import ParameterSchema as PS

    param = PS(
        id=uuid4(),
        create_date=stamp,
        modified_date=stamp,
        template=group_schema,
        values={"Volume": 25.0},
    )

    # Override the unit
    param.volume.unit = "uL"

    # Bracket assignment by slug — unit must be preserved
    param.values["volume"] = 50.0
    assert param.volume.value == pytest.approx(50.0)
    assert param.volume.unit == "uL"

    # Bracket assignment by original name — same behaviour
    param.values["Volume"] = 99.0
    assert param.volume.value == pytest.approx(99.0)
    assert param.volume.unit == "uL"


def test_property_schema_value_assignment_preserves_overridden_unit():
    volume = _attribute_template_schema("Volume", "float", 10.0, unit="uL")
    group_schema = _attribute_group_schema("Content", [volume])
    stamp = _now()
    prop = PropertySchema(
        id=uuid4(),
        create_date=stamp,
        modified_date=stamp,
        template=group_schema,
        values={"Volume": 10.0},
    )

    # Confirm template default unit
    assert prop.volume.unit == "uL"

    # Override the unit
    prop.volume.unit = "mL"
    assert prop.volume.unit == "mL"

    # Update the value directly — unit must remain "mL", not revert to "uL"
    prop.volume = 50
    assert prop.volume.value == pytest.approx(50.0)
    assert prop.volume.unit == "mL"


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


def test_parameter_schema_respects_metadata_bounds():
    voltage = _attribute_template_schema(
        "Voltage", "int", 5, metadata={"min": 0, "max": 10}
    )
    group_schema = _attribute_group_schema("Inputs", [voltage])
    stamp = _now()

    with pytest.raises(ValueError):
        ParameterSchema(
            id=uuid4(),
            create_date=stamp,
            modified_date=stamp,
            template=group_schema,
            values={"Voltage": 11},
        )


def test_enum_attribute_validates_choices():
    drop = _attribute_template_schema(
        "Drop Position",
        "enum",
        "u",
        metadata={"choices": {"u": {"x": 0, "y": 1}, "d": {"x": 0, "y": -1}}},
    )
    group_schema = _attribute_group_schema("Positions", [drop])
    stamp = _now()

    schema = ParameterSchema(
        id=uuid4(),
        create_date=stamp,
        modified_date=stamp,
        template=group_schema,
        values={"Drop Position": "d"},
    )
    assert schema.values.get("Drop Position").value == "d"

    with pytest.raises(ValueError):
        ParameterSchema(
            id=uuid4(),
            create_date=stamp,
            modified_date=stamp,
            template=group_schema,
            values={"Drop Position": "x"},
        )
