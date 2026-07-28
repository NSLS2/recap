from datetime import datetime
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from recap.adapter.transport import (
    QueryRequest,
    QueryResult,
    _restore_metadata,
    hydrate_result,
    serialize_model,
)
from recap.db.process import ProcessRun
from recap.dsl.query import (
    Field,
    FieldOrdering,
    FieldPredicate,
    PropertyFilter,
    QuerySpec,
)
from recap.schemas.attribute import (
    AttributeGroupTemplateSchema,
)
from recap.schemas.common import StepStatus
from recap.schemas.process import ProcessRunSchema
from recap.schemas.resource import ResourceSchema
from recap.tests.transport_factories import (
    STAMP,
    attribute_group,
    full_process_run,
    full_resource,
    minimal_resource,
)


def test_query_spec_normalizes_legacy_full_load_mode():
    with pytest.warns(DeprecationWarning, match="load='eager'"):
        spec = QuerySpec.model_validate({"load_mode": "full"})

    assert spec.load_mode == "eager"


def test_query_request_serializes_complete_supported_query_spec():
    parent_id = uuid4()
    spec = QuerySpec(
        filters={"name": "sample"},
        preloads=("properties", "children"),
        limit=25,
        offset=5,
        property_filters=[PropertyFilter(name="temperature", value=20)],
        parent_resource_id=parent_id,
        include_archived=True,
        load_mode="eager",
        on_unloaded="raise",
    )

    request = QueryRequest.from_query(
        ResourceSchema, spec, namespace_path="beamline/amx"
    )

    assert request.schema_name == "ResourceSchema"
    assert request.namespace_path == "beamline/amx"
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
        "include_archived": True,
        "local_metadata_filters": {},
        "effective_metadata_filters": {},
        "load_mode": "eager",
        "on_unloaded": "raise",
    }
    assert request.spec["load_mode"] == "eager"
    assert "full" not in request.spec.values()


def test_query_request_serializes_structured_predicates_and_orderings():
    namespace_id = uuid4()
    request = QueryRequest.from_query(
        ProcessRunSchema,
        QuerySpec(
            predicates=[
                Field("namespace_id") == namespace_id,
                Field("create_date") >= STAMP,
            ],
            orderings=[Field("create_date").desc()],
        ),
        namespace_path="beamline/amx",
    )

    assert request.spec["predicates"] == [
        {"field": "namespace_id", "op": "eq", "value": str(namespace_id)},
        {
            "field": "create_date",
            "op": "gte",
            "value": STAMP.isoformat().replace("+00:00", "Z"),
        },
    ]
    reconstructed = QuerySpec.model_validate(request.spec)
    assert all(
        isinstance(predicate, FieldPredicate) for predicate in reconstructed.predicates
    )
    assert isinstance(reconstructed.orderings[0], FieldOrdering)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("predicates", (ProcessRun.name == "sample",)),
        (
            "predicates",
            (Field("name") == "sample", ProcessRun.name == "sample"),
        ),
        ("orderings", (ProcessRun.name,)),
        ("orderings", (Field("name").asc(), ProcessRun.name.desc())),
    ],
)
def test_query_request_rejects_every_legacy_query_feature(field, value):
    with pytest.raises(TypeError, match="Field"):
        QueryRequest.from_query(
            ResourceSchema,
            QuerySpec(**{field: value}),
            namespace_path="beamline/amx",
        )


def test_datetime_default_value_round_trips_as_datetime():
    group = attribute_group()

    payload = serialize_model(group)
    [hydrated] = hydrate_result(
        AttributeGroupTemplateSchema,
        QueryResult(schema_name="AttributeGroupTemplateSchema", items=[payload]),
    )

    assert payload["attribute_templates"][0]["default_value"] == {
        "__recap_transport_scalar_v1__": "datetime",
        "value": STAMP.isoformat(),
    }
    assert hydrated.attribute_templates[0].default_value == STAMP
    assert isinstance(hydrated.attribute_templates[0].default_value, datetime)


def test_metadata_restoration_rejects_sequence_length_mismatch():
    resource = minimal_resource()
    payload = serialize_model(resource)

    with pytest.raises(ValueError, match="zip"):
        _restore_metadata([resource], [payload, payload])


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
    assert hydrated.namespace_id == run.namespace_id
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
