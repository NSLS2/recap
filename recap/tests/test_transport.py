from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from recap.adapter.transport import (
    QueryRequest,
    QueryResult,
    hydrate_result,
    serialize_model,
)
from recap.dsl.query import PropertyFilter, QuerySpec
from recap.schemas.attribute import (
    AttributeGroupTemplateSchema,
    AttributeTemplateSchema,
)
from recap.schemas.common import StepStatus
from recap.schemas.process import ProcessRunSchema, ProcessTemplateSchema
from recap.schemas.resource import (
    PropertySchema,
    ResourceAssignmentSchema,
    ResourceRef,
    ResourceSchema,
    ResourceSlotSchema,
    ResourceTemplateRef,
    ResourceTemplateSchema,
    ResourceTypeSchema,
)
from recap.schemas.step import ParameterSchema, StepSchema, StepTemplateSchema
from recap.utils.general import Direction

STAMP = datetime(2026, 7, 23, 12, 30, tzinfo=UTC)


def fields(**values):
    return {
        "id": uuid4(),
        "create_date": STAMP,
        "modified_date": STAMP,
        **values,
    }


def attribute_group(name: str = "Measurements") -> AttributeGroupTemplateSchema:
    attribute = AttributeTemplateSchema(
        **fields(
            name="Captured At",
            slug="captured_at",
            value_type="datetime",
            unit=None,
            default_value=STAMP,
            metadata={"source": "detector"},
        )
    )
    return AttributeGroupTemplateSchema(
        **fields(name=name, slug=name.lower(), attribute_templates=[attribute])
    )


def resource_template(
    group: AttributeGroupTemplateSchema | None = None,
) -> ResourceTemplateSchema:
    parent = ResourceTemplateRef(
        **fields(name="Container", slug="container", version="1.0", types=[])
    )
    return ResourceTemplateSchema(
        **fields(
            name="Sample",
            slug="sample",
            version="1.0",
            types=[],
            parent=parent,
            children={},
            attribute_group_templates=[] if group is None else [group],
        )
    )


def minimal_resource(name: str = "sample") -> ResourceSchema:
    resource = ResourceSchema(
        **fields(
            name=name,
            template=resource_template(),
            parent=None,
            children={},
            properties={},
        )
    )
    return resource.set_loaded_relations(
        {"children": False, "properties": False}, on_unloaded="silent"
    )


def full_resource() -> ResourceSchema:
    group = attribute_group()
    template = resource_template(group)
    parent_template = ResourceTemplateRef(
        **fields(name="Container", slug="container", version="1.0", types=[])
    )
    parent = ResourceRef(**fields(name="plate", template=parent_template))
    prop = PropertySchema(**fields(template=group, values={"Captured At": STAMP}))
    child = minimal_resource("child")
    resource = ResourceSchema(
        **fields(
            name="sample",
            template=template,
            parent=parent,
            children={"child": child},
            properties={"Measurements": prop},
        )
    )
    return resource.set_loaded_relations(
        {"children": True, "properties": True}, on_unloaded="raise"
    )


def full_process_run() -> ProcessRunSchema:
    group = attribute_group("Acquisition")
    resource_type = ResourceTypeSchema(**fields(name="sample"))
    slot = ResourceSlotSchema(
        **fields(
            name="input",
            resource_type=resource_type,
            direction=Direction.input,
            required=True,
        )
    )
    step_template = StepTemplateSchema(
        **fields(
            name="Acquire",
            attribute_group_templates=[group],
            resource_slots={"input": slot},
        )
    )
    process_template = ProcessTemplateSchema(
        **fields(
            name="Measurement",
            version="1.0",
            is_active=True,
            step_templates={"Acquire": step_template},
            resource_slots=[slot],
        )
    )
    parameter = ParameterSchema(**fields(template=group, values={"Captured At": STAMP}))
    resource = minimal_resource()
    run_id = uuid4()
    step = StepSchema(
        **fields(
            name="Acquire",
            template=step_template,
            parameters={"Acquisition": parameter},
            state=StepStatus.COMPLETE,
            process_run_id=run_id,
            resources={"input": resource},
        )
    )
    assignment = ResourceAssignmentSchema(slot=slot, resource=resource, step_id=step.id)
    run = ProcessRunSchema(
        **fields(
            id=run_id,
            name="run-1",
            description="transport round trip",
            campaign_id=uuid4(),
            template=process_template,
            steps={"Acquire": step},
            assigned_resources={"input": assignment},
        )
    )
    return run.set_loaded_relations(
        {"steps": True, "assigned_resources": True}, on_unloaded="raise"
    )


def test_query_request_serializes_complete_supported_query_spec():
    campaign_id = uuid4()
    parent_id = uuid4()
    spec = QuerySpec(
        filters={"name": "sample"},
        preloads=("properties", "children"),
        limit=25,
        offset=5,
        property_filters=[PropertyFilter(name="temperature", value=20)],
        parent_resource_id=parent_id,
        campaign_id=campaign_id,
        load_mode="full",
        on_unloaded="raise",
    )

    request = QueryRequest.from_query(ResourceSchema, spec)

    assert request.schema_name == "ResourceSchema"
    assert request.spec == {
        "filters": {"name": "sample"},
        "predicates": [],
        "orderings": [],
        "preloads": ["properties", "children"],
        "limit": 25,
        "offset": 5,
        "property_filters": [
            {
                "name": "temperature",
                "group": None,
                "op": "eq",
                "value": 20,
                "upper": None,
                "value_type": None,
            }
        ],
        "parent_resource_id": str(parent_id),
        "parameter_filters": [],
        "campaign_id": str(campaign_id),
        "load_mode": "full",
        "on_unloaded": "raise",
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("predicates", (lambda item: True,), "predicates"),
        ("orderings", (lambda item: item.name,), "orderings"),
    ],
)
def test_query_request_rejects_non_transportable_query_features(field, value, message):
    with pytest.raises(NotImplementedError, match=message):
        QueryRequest.from_query(ResourceSchema, QuerySpec(**{field: value}))


def test_resource_round_trip_preserves_full_graph_and_transport_state():
    resource = full_resource()

    payload = serialize_model(resource)

    assert payload["id"] == str(resource.id)
    assert payload["create_date"] == STAMP.isoformat()
    assert payload["template"]["parent"]["name"] == "Container"
    assert payload["parent"]["name"] == "plate"
    assert payload["properties"]["Measurements"]["template"]["attribute_templates"][0][
        "metadata_json"
    ] == {"source": "detector"}
    assert (
        payload["properties"]["Measurements"]["values"]["Captured At"]["value"]
        == STAMP.isoformat()
    )
    assert payload["__recap__"] == {
        "loaded_relations": {"children": True, "properties": True},
        "on_unloaded": "raise",
    }
    assert payload["children"]["child"]["__recap__"]["on_unloaded"] == "silent"

    [hydrated] = hydrate_result(
        ResourceSchema,
        QueryResult(schema_name="ResourceSchema", items=[payload]),
    )

    assert isinstance(hydrated.id, UUID)
    assert hydrated.id == resource.id
    assert hydrated.create_date == STAMP
    assert hydrated.name == resource.name
    assert hydrated.template.parent == resource.template.parent
    assert hydrated.parent == resource.parent
    assert isinstance(hydrated.properties, BaseModel)
    assert hydrated.properties.measurements.captured_at.value == STAMP
    assert hydrated._loaded_relations == {"children": True, "properties": True}
    assert hydrated._on_unloaded == "raise"
    assert hydrated.children["child"]._loaded_relations == {
        "children": False,
        "properties": False,
    }
    assert hydrated.children["child"]._on_unloaded == "silent"


def test_minimal_resource_round_trip_does_not_trigger_unloaded_guard():
    resource = minimal_resource()
    resource.set_loaded_relations(
        {"children": False, "properties": False}, on_unloaded="raise"
    )

    payload = serialize_model(resource)
    [hydrated] = hydrate_result(
        ResourceSchema,
        QueryResult(schema_name="ResourceSchema", items=[payload]),
    )

    assert hydrated.__dict__["children"] == {}
    assert hydrated.__dict__["properties"] == {}
    assert hydrated._loaded_relations == {"children": False, "properties": False}
    assert hydrated._on_unloaded == "raise"


def test_process_run_round_trip_restores_enums_dynamic_parameters_and_nested_state():
    run = full_process_run()

    payload = serialize_model(run)

    assert payload["steps"]["Acquire"]["state"] == "COMPLETE"
    assert payload["template"]["resource_slots"][0]["direction"] == "input"
    assert payload["__recap__"]["loaded_relations"] == {
        "steps": True,
        "assigned_resources": True,
    }
    nested_resource = payload["assigned_resources"]["input"]["resource"]
    assert nested_resource["__recap__"]["loaded_relations"] == {
        "children": False,
        "properties": False,
    }

    [hydrated] = hydrate_result(
        ProcessRunSchema,
        QueryResult(schema_name="ProcessRunSchema", items=[payload]),
    )

    assert hydrated.id == run.id
    assert hydrated.campaign_id == run.campaign_id
    assert hydrated.create_date == STAMP
    assert hydrated.steps["Acquire"].state is StepStatus.COMPLETE
    assert isinstance(hydrated.steps["Acquire"].parameters, BaseModel)
    assert hydrated.steps["Acquire"].parameters.acquisition.captured_at.value == STAMP
    assigned = hydrated.assigned_resources["input"].resource
    assert assigned._loaded_relations == {"children": False, "properties": False}
    assert assigned._on_unloaded == "silent"
    assert hydrated._loaded_relations == {
        "steps": True,
        "assigned_resources": True,
    }
    assert hydrated._on_unloaded == "raise"


def test_minimal_process_run_round_trip_restores_private_state():
    run = full_process_run()
    run.__dict__["steps"] = {}
    run.__dict__["assigned_resources"] = {}
    run.set_loaded_relations(
        {"steps": False, "assigned_resources": False}, on_unloaded="silent"
    )

    payload = serialize_model(run)
    [hydrated] = hydrate_result(
        ProcessRunSchema,
        QueryResult(schema_name="ProcessRunSchema", items=[payload]),
    )

    assert hydrated.__dict__["steps"] == {}
    assert hydrated.__dict__["assigned_resources"] == {}
    assert hydrated._loaded_relations == {
        "steps": False,
        "assigned_resources": False,
    }
    assert hydrated._on_unloaded == "silent"
