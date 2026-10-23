from datetime import datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from recap.adapter.entity_batch import EntityBatchCollector
from recap.adapter.entity_records import EntityBatch, EntityRef, ResourceRecord
from recap.adapter.transport import (
    FlatQueryResult,
    QueryRequest,
    decode_query_batch,
    encode_query_batch,
)
from recap.client.identity import IdentityMap
from recap.client.materialization import BatchMaterializer
from recap.db.process import ProcessRun
from recap.dsl.query import (
    Field,
    FieldOrdering,
    FieldPredicate,
    PropertyFilter,
    QuerySpec,
)
from recap.exceptions import RecapProtocolError
from recap.schemas.process import ProcessRunSchema
from recap.schemas.resource import ResourceSchema

from .entity_fixtures import _seed_resource_graph
from .test_process_run_resource_tree_perf import _seed_run_with_resource_chain
from .transport_factories import STAMP


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
    expected = spec.model_dump(mode="json")
    expected.pop("include_mutable")
    assert request.spec == expected
    assert request.entity == "resource"
    assert request.projection == "full"
    assert request.namespace_path == "beamline/amx"
    assert "schema_name" not in request.model_dump()


def test_query_request_serializes_include_mutable_when_true():
    request = QueryRequest.from_query(
        ResourceSchema, QuerySpec(include_mutable=True), namespace_path="beamline/amx"
    )
    assert request.spec["include_mutable"] is True


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
    "field,value",
    [
        ("predicates", (ProcessRun.name == "sample",)),
        ("predicates", (Field("name") == "sample", ProcessRun.name == "sample")),
        ("orderings", (ProcessRun.name,)),
        ("orderings", (Field("name").asc(), ProcessRun.name.desc())),
    ],
)
def test_query_request_rejects_sqlalchemy_expressions(field, value):
    with pytest.raises(TypeError, match="Field"):
        QueryRequest.from_query(
            ResourceSchema, QuerySpec(**{field: value}), namespace_path="scope"
        )


def test_flat_result_requires_projection_and_rejects_nested_items():
    with pytest.raises(ValidationError):
        FlatQueryResult.model_validate(
            {"entity": "resource", "roots": [], "entities": [], "adjacency": []}
        )
    with pytest.raises(ValidationError):
        FlatQueryResult.model_validate(
            {"entity": "resource", "projection": "full", "items": []}
        )
    result = FlatQueryResult(
        entity="process_run", projection="full", roots=[], entities=[], adjacency=[]
    )
    with pytest.raises(RecapProtocolError, match="does not match"):
        decode_query_batch(ResourceSchema, result)


def _roundtrip(schema, batch):
    envelope = encode_query_batch(batch, entity=batch.roots[0].kind, projection="full")
    return decode_query_batch(
        schema, FlatQueryResult.model_validate_json(envelope.model_dump_json())
    )


def test_flat_resource_roundtrip_preserves_cycles_values_and_load_state(db_session):
    parent, child, _, _ = _seed_resource_graph(db_session)
    template = parent.properties["Measurements"].template.attribute_templates[0]
    template.value_type = "datetime"
    template.default_value = STAMP.isoformat()
    stored = parent.properties["Measurements"]._values["dose"]
    stored.value_json = STAMP.isoformat()
    stored.unit = "s"
    stored.metadata_json = {"source": "scan"}
    collector = EntityBatchCollector(on_unloaded="raise")
    collector.add_root(parent, {"template", "parent", "children", "properties"})
    with db_session.no_autoflush:
        batch = collector.collect()
    decoded = _roundtrip(ResourceSchema, batch)
    model = BatchMaterializer(IdentityMap()).materialize(ResourceSchema, decoded)[0]
    assert model.children["child"].id == child.id
    assert model.children["child"].parent is model
    assert model.properties.measurements.dose.value == STAMP
    assert model.properties.measurements.dose.metadata_json == {"source": "scan"}
    assert (
        model.template.attribute_group_templates[0].attribute_templates[0].default_value
        == STAMP
    )
    assert model.is_loaded("children")
    assert model._on_unloaded == "raise"


def test_flat_process_roundtrip_preserves_parameters_and_assignments(db_session):
    run = _seed_run_with_resource_chain(db_session, name="transport-run", depth=2)
    collector = EntityBatchCollector(on_unloaded="silent")
    collector.add_root(run, {"template", "steps", "steps.parameters", "resources"})
    decoded = _roundtrip(ProcessRunSchema, collector.collect())
    model = BatchMaterializer(IdentityMap()).materialize(ProcessRunSchema, decoded)[0]
    step = next(iter(model.steps.values()))
    assert next(iter(step.parameters.values())).values.dwell.value == 1
    assert (
        step.resources["input"]
        is next(iter(model.assigned_resources.values())).resource
    )
    assert model.is_loaded("steps")
    assert model._on_unloaded == "silent"


def test_flat_minimal_resource_roundtrip_preserves_unloaded_guards(db_session):
    parent, _, _, _ = _seed_resource_graph(db_session)
    collector = EntityBatchCollector(on_unloaded="raise")
    collector.add_root(parent, set())
    decoded = _roundtrip(ResourceSchema, collector.collect())
    model = BatchMaterializer(IdentityMap()).materialize(ResourceSchema, decoded)[0]
    assert model.__dict__["children"] == {}
    assert model.__dict__["properties"] == {}
    assert not model.is_loaded("children")
    assert model._on_unloaded == "raise"


def test_flat_batch_roundtrip_preserves_arbitrary_datetime_values_and_metadata():
    stamp = datetime(2026, 1, 1, 12, 30)
    ref = EntityRef(kind="resource", id=uuid4())
    batch = EntityBatch(
        roots=[ref],
        entities=[
            ResourceRecord(
                id=ref.id,
                revision=1,
                fields={
                    "metadata": {
                        "timestamp": stamp,
                        "sequence": [stamp, None, stamp],
                        "$ref": "user data",
                    }
                },
            )
        ],
        adjacency=[],
    )
    assert (
        _roundtrip(ResourceSchema, batch).entities[0].fields == batch.entities[0].fields
    )
