from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from recap.adapter import hydration_fields as fields
from recap.adapter import hydration_relations as relations
from recap.schemas.attribute import (
    AttributeGroupTemplateSchema,
    AttributeTemplateSchema,
)
from recap.schemas.resource import ResourceSchema, ResourceSlotSchema


class ScalarRow:
    id = uuid4()
    create_date = datetime(2026, 1, 1)
    modified_date = create_date
    namespace_id = uuid4()
    status = "MUTABLE"
    revision = 1
    name = "sample"

    def __getattr__(self, name):
        if name in {"children", "template", "properties", "parent"}:
            raise AssertionError(f"Unexpected relationship read: {name}")
        raise AttributeError(name)


def test_scalar_extraction_does_not_read_relationships():
    values = fields.extract_simple_fields(ResourceSchema, ScalarRow())
    assert values["name"] == "sample"
    assert values["revision"] == 1
    assert not {"children", "template", "properties", "parent"} & values.keys()
    assert "description" not in values
    slot = fields.extract_model_fields(
        ResourceSlotSchema, SimpleNamespace(name="input", direction="INPUT")
    )
    assert slot == {"name": "input", "direction": "INPUT"}


def test_validation_preserves_orm_aliases_without_relationship_access():
    values = fields.extract_model_fields(
        AttributeTemplateSchema,
        SimpleNamespace(
            id=uuid4(),
            create_date=datetime(2026, 1, 1),
            modified_date=datetime(2026, 1, 1),
            name="Dose",
            slug="dose",
            value_type="int",
            unit="uL",
            default_value=None,
            metadata="not the ORM metadata column",
            metadata_json={"min": 0},
        ),
    )
    validated = fields.validate_fields(AttributeTemplateSchema, values)
    assert validated.metadata == {"min": 0}
    assert values["metadata"] == {"min": 0}
    assert validated.default_value is None


def test_trusted_construction_is_separate_from_validation():
    values = fields.extract_simple_fields(ResourceSchema, ScalarRow())
    values["revision"] = "unvalidated"
    model = fields.construct_model(ResourceSchema, values, children={})
    assert model.revision == "unvalidated"
    assert "children" not in values
    with pytest.raises(ValidationError):
        fields.validate_fields(ResourceSchema, values)


def test_attribute_extraction_preserves_nulls_units_metadata_and_missing_values():
    stamp = datetime(2026, 1, 1)
    template = AttributeGroupTemplateSchema.model_construct(
        id=uuid4(),
        create_date=stamp,
        modified_date=stamp,
        name="Dose",
        slug="dose",
        attribute_templates=[
            AttributeTemplateSchema.model_construct(
                name=name, slug=name, value_type="array", metadata={}, unit="uL"
            )
            for name in ("observed", "missing")
        ],
    )
    source = SimpleNamespace(
        _values={
            "observed": SimpleNamespace(
                value=None, unit="mL", metadata_json={"source": "scan"}
            )
        }
    )
    payload = fields.extract_attribute_values(source, template)
    assert payload == {
        "observed": {"value": None, "unit": "mL", "metadata_json": {"source": "scan"}},
        "missing": {"value": None, "unit": "uL", "metadata_json": {}},
    }
    assert isinstance(payload["observed"], dict)
    model = fields.construct_attribute_values(template, payload)
    assert model.observed.value is None
    assert model.observed.unit == "mL"
    assert model.missing.unit == "uL"
    assert source._values["observed"].value is None


def test_requested_relationship_extraction_does_not_materialize_models():
    child = ScalarRow()
    row = SimpleNamespace(children={"well": child})
    extracted = relations.resource_relations(row, {"children"})
    assert extracted == {"children": {"well": child}}
    assert extracted["children"]["well"] is child
    assert relations.resource_relations(ScalarRow(), set()) == {}
    assert relations.resource_template_relations(row, {"children"}) == extracted
    assert relations.process_template_relations(
        SimpleNamespace(step_templates={"collect": child}), {"step_templates"}
    ) == {"step_templates": {"collect": child}}
    binding = SimpleNamespace(role="input", resource_slot=child)
    assert relations.step_template_relations(
        SimpleNamespace(bindings={uuid4(): binding}), {"resource_slots"}
    ) == {"resource_slots": {"input": child}}


def test_flat_child_indexing_does_not_access_child_relationships():
    parent_id = uuid4()
    child = SimpleNamespace(id=uuid4(), parent_id=parent_id, name="well")
    root = SimpleNamespace(id=parent_id, parent_id=None)
    index = relations.index_children([root, child])
    assert index == {parent_id: [child]}
    extracted = relations.resource_relations(root, {"children"}, index)
    assert extracted == {"children": {"well": child}}
    assert relations.resource_relations(child, {"children"}, index) == {"children": {}}


def test_run_and_step_projection_preserves_relationship_keys():
    step = SimpleNamespace(name="collect", id=uuid4(), parent_id=None)
    run = SimpleNamespace(steps={step.id: step})
    assert relations.process_run_relations(run, {"steps"}) == {"steps": {step.id: step}}
    assert relations.step_relations(
        SimpleNamespace(resources={"input": run}, children=[step]),
        {"resources", "children"},
    ) == {"resources": {"input": run}, "children": [step]}
    assert relations.process_run_relations(ScalarRow(), set()) == {}
    assert relations.step_relations(ScalarRow(), set()) == {}


@pytest.mark.parametrize("child", [False, True])
def test_included_step_projection_reads_assignments_only_for_resource_overrides(child):
    row = SimpleNamespace(
        parent_id=uuid4() if child else None,
        template=object(),
        children=[],
        parameters={},
        resources={},
        assignments={},
    )
    minimal = relations.included_step_relations(
        row, include_parameters=False, include_resources=False
    )
    assert set(minimal) == {"template", "children"}
    included = relations.included_step_relations(
        row, include_parameters=True, include_resources=True
    )
    assert set(included) == (
        {"template", "children", "parameters", "resources", "assignments"}
        if child
        else {"template", "children", "parameters", "resources"}
    )
