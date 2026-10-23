import pytest
from pydantic import ValidationError

from recap.adapter.entity_batch import EntityBatchCollector
from recap.adapter.entity_records import EntityRef, SingleAdjacency
from recap.adapter.transport import (
    FlatQueryResult,
    decode_query_batch,
    encode_query_batch,
)
from recap.client.identity import IdentityMap
from recap.client.materialization import BatchMaterializer
from recap.exceptions import RecapProtocolError
from recap.schemas.process import ProcessRunSchema
from recap.schemas.resource import ResourceSchema

from .test_hydration_cache import _seed_resource_graph
from .test_process_run_resource_tree_perf import _seed_run_with_resource_chain


@pytest.fixture(params=["native", "wire"])
def materializer(request):
    class ParityMaterializer(BatchMaterializer):
        def materialize(self, schema, batch):
            if request.param == "wire":
                envelope = encode_query_batch(
                    batch, entity=batch.roots[0].kind, projection="full"
                )
                batch = decode_query_batch(
                    schema,
                    FlatQueryResult.model_validate_json(envelope.model_dump_json()),
                )
            return super().materialize(schema, batch)

    return ParityMaterializer(IdentityMap())


def _batch(parent, *, requested=None):
    collector = EntityBatchCollector()
    collector.add_root(
        parent, requested or {"template", "parent", "children", "properties"}
    )
    from sqlalchemy.orm import object_session

    with object_session(parent).no_autoflush:
        return collector.collect()


def test_batch_materialization_preserves_cycles_identity_and_typed_values(
    db_session, materializer
):
    parent, child, _, _ = _seed_resource_graph(db_session)
    first = materializer.materialize(ResourceSchema, _batch(parent))[0]
    second = materializer.materialize(ResourceSchema, _batch(parent))[0]
    assert first is second
    assert first.children["child"].parent is first
    assert first.children["child"].id == child.id
    assert first.properties.measurements.dose.value == 1
    assert first.properties is second.properties
    assert first.properties.measurements.values is second.properties.measurements.values


def test_newer_child_updates_under_unchanged_parent(
    db_session,
    materializer,
):
    parent, child, _, _ = _seed_resource_graph(db_session)
    first = materializer.materialize(ResourceSchema, _batch(parent))[0]
    original_child = first.children["child"]
    child.name = "updated"
    child.revision += 1
    second = materializer.materialize(ResourceSchema, _batch(parent))[0]
    assert second is first
    assert original_child.name == "updated"
    assert original_child.revision == child.revision


def test_partial_reads_do_not_downgrade_loaded_data_and_older_reads_are_ignored(
    db_session,
    materializer,
):
    parent, _, _, _ = _seed_resource_graph(db_session)
    first = materializer.materialize(ResourceSchema, _batch(parent))[0]
    children = first.children
    partial = _batch(parent, requested={"template"})
    parent_record = next(
        record for record in partial.entities if record.id == parent.id
    )
    parent_record.revision = 0
    parent_record.fields["name"] = "stale"
    second = materializer.materialize(ResourceSchema, partial)[0]
    assert second.name == "parent"
    assert second.children is children
    assert second.is_loaded("children")


def test_property_updates_preserve_generated_references_and_null_metadata(
    db_session, materializer
):
    parent, _, _, _ = _seed_resource_graph(db_session)
    first = materializer.materialize(ResourceSchema, _batch(parent))[0]
    properties = first.properties
    group = properties.measurements
    values = group.values
    dose = values.dose
    stored = parent.properties["Measurements"]._values["dose"]
    stored.value_json = None
    stored.unit = "mL"
    stored.metadata_json = {"source": "new"}
    parent.revision += 1
    result = materializer.materialize(ResourceSchema, _batch(parent))[0]
    assert result.properties is properties
    assert properties.measurements is group
    assert group.values is values
    assert values.dose is dose
    assert dose.value is None
    assert dose.unit == "mL"
    assert dose.metadata_json == {"source": "new"}


def test_invalid_reference_fails_before_identity_map_mutation(db_session):
    parent, _, _, _ = _seed_resource_graph(db_session)
    batch = _batch(parent)
    batch.adjacency.append(
        SingleAdjacency(
            source=batch.roots[0],
            relation="template",
            target=EntityRef(kind="resource", id=parent.id),
        )
    )
    identities = IdentityMap()
    with pytest.raises(RecapProtocolError):
        BatchMaterializer(identities).materialize(ResourceSchema, batch)
    assert identities.get(("resource", parent.id)) is None


def test_missing_and_duplicate_records_are_rejected(db_session):
    parent, _, _, _ = _seed_resource_graph(db_session)
    batch = _batch(parent)
    batch.entities.append(batch.entities[0])
    with pytest.raises(RecapProtocolError, match="Duplicate"):
        BatchMaterializer(IdentityMap()).materialize(ResourceSchema, batch)
    batch.entities = []
    with pytest.raises(RecapProtocolError):
        BatchMaterializer(IdentityMap()).materialize(ResourceSchema, batch)


def test_invalid_typed_value_does_not_change_cached_entity(db_session, materializer):
    parent, _, _, _ = _seed_resource_graph(db_session)
    first = materializer.materialize(ResourceSchema, _batch(parent))[0]
    incoming = _batch(parent)
    for record in incoming.entities:
        if record.kind == "resource" and record.id == parent.id:
            record.fields["name"] = "invalid refresh"
        if record.kind == "attribute_value":
            record.fields["value"] = "not an integer"
    with pytest.raises(ValidationError):
        materializer.materialize(ResourceSchema, incoming)
    assert first.name == "parent"
    assert first.properties.measurements.dose.value == 1


def test_complete_membership_replacement_and_partial_extension(
    db_session, materializer
):
    parent, child, _, _ = _seed_resource_graph(db_session)
    first = materializer.materialize(ResourceSchema, _batch(parent))[0]
    partial = _batch(parent)
    edge = next(
        edge
        for edge in partial.adjacency
        if edge.source.id == parent.id and edge.relation == "children"
    )
    edge.targets = {}
    edge.complete = False
    materializer.materialize(ResourceSchema, partial)
    assert first.children["child"].id == child.id
    edge.complete = True
    materializer.materialize(ResourceSchema, partial)
    assert first.children == {}


def test_array_values_preserve_order_repetition_and_detached_draft(
    db_session, materializer
):
    parent, _, _, _ = _seed_resource_graph(db_session)
    template = parent.properties["Measurements"].template.attribute_templates[0]
    template.value_type = "array"
    template.default_value = "[]"
    stored = parent.properties["Measurements"]._values["dose"]
    stored.value_json = [1, 2, 1, 2]
    first = materializer.materialize(ResourceSchema, _batch(parent))[0]
    draft = first.model_copy(deep=True)
    draft.properties.measurements.dose.value.append(3)
    stored.value_json = [4, 4]
    parent.revision += 1
    materializer.materialize(ResourceSchema, _batch(parent))
    assert first.properties.measurements.dose.value == [4, 4]
    assert draft.properties.measurements.dose.value == [1, 2, 1, 2, 3]


def test_first_load_requires_scalar_fields_but_refresh_preserves_omissions(
    db_session, materializer
):
    parent, _, _, _ = _seed_resource_graph(db_session)
    batch = _batch(parent)
    record = next(
        record
        for record in batch.entities
        if record.kind == "resource" and record.id == parent.id
    )
    del record.fields["name"]
    with pytest.raises(RecapProtocolError, match="Missing required.*name"):
        materializer.materialize(ResourceSchema, batch)
    assert materializer.identity_map.get(("resource", parent.id)) is None
    first = materializer.materialize(ResourceSchema, _batch(parent))[0]
    assert materializer.materialize(ResourceSchema, batch)[0] is first
    assert first.name == "parent"


def test_unknown_attribute_name_rejected_without_cached_mutation(
    db_session, materializer
):
    parent, _, _, _ = _seed_resource_graph(db_session)
    first = materializer.materialize(ResourceSchema, _batch(parent))[0]
    properties, group, values = (
        first.properties,
        first.properties.measurements,
        first.properties.measurements.values,
    )
    batch = _batch(parent)
    record = next(
        record
        for record in batch.entities
        if record.kind == "resource" and record.id == parent.id
    )
    record.fields["name"] = "must not apply"
    edge = next(edge for edge in batch.adjacency if edge.relation == "values")
    edge.targets["unknown"] = edge.targets["dose"]
    with pytest.raises(RecapProtocolError, match="Unknown attribute"):
        materializer.materialize(ResourceSchema, batch)
    assert first.name == "parent"
    assert first.properties is properties
    assert properties.measurements is group
    assert group.values is values


def test_template_type_change_replaces_value_container_and_validation(
    db_session, materializer
):
    parent, _, _, _ = _seed_resource_graph(db_session)
    first = materializer.materialize(ResourceSchema, _batch(parent))[0]
    old_values = first.properties.measurements.values
    batch = _batch(parent)
    attribute = next(
        record for record in batch.entities if record.kind == "attribute_template"
    )
    attribute.fields.update(value_type="str", default_value="text")
    value = next(
        record for record in batch.entities if record.kind == "attribute_value"
    )
    value.fields["value"] = "new type"
    materializer.materialize(ResourceSchema, batch)
    current = first.properties.measurements.values
    assert current is not old_values
    assert current.dose.value == "new type"
    current.dose.value = "another string"
    assert old_values.dose.value == 1


def test_partial_value_adjacency_preserves_omitted_values(db_session, materializer):
    parent, _, _, _ = _seed_resource_graph(db_session)
    first = materializer.materialize(ResourceSchema, _batch(parent))[0]
    batch = _batch(parent)
    edge = next(edge for edge in batch.adjacency if edge.relation == "values")
    edge.targets = {}
    edge.complete = False
    materializer.materialize(ResourceSchema, batch)
    assert first.properties.measurements.dose.value == 1


@pytest.mark.parametrize(
    "value_type,value,metadata",
    [
        ("int", "7", {"min": 0, "max": 10}),
        ("float", "2.5", {"min": 0}),
        ("enum", "ready", {"choices": ["ready", "done"]}),
        ("datetime", "2026-10-23T12:30:00+00:00", {}),
        ("array", [2, 1, 2], {}),
        ("bool", "true", {}),
    ],
)
def test_attribute_types_and_defaults_match_native_and_wire(
    db_session, materializer, value_type, value, metadata
):
    parent, _, _, _ = _seed_resource_graph(db_session)
    batch = _batch(parent)
    attribute = next(
        record for record in batch.entities if record.kind == "attribute_template"
    )
    attribute.fields.update(
        value_type=value_type, default_value=value, metadata=metadata
    )
    stored = next(
        record for record in batch.entities if record.kind == "attribute_value"
    )
    stored.fields.update(
        value=value, unit="mL", metadata_json={"source": ["scan", "scan"]}
    )
    result = materializer.materialize(ResourceSchema, batch)[0]
    from recap.utils.general import from_json_value

    dose = result.properties.measurements.dose
    assert dose.value == from_json_value(value_type, value)
    assert dose.unit == "mL"
    assert dose.metadata_json == {"source": ["scan", "scan"]}
    assert (
        result.template.attribute_group_templates[0]
        .attribute_templates[0]
        .default_value
        == dose.value
    )


@pytest.mark.parametrize(
    "value,metadata", [(11, {"max": 10}), ("bad", {"choices": ["good"]})]
)
def test_bounds_and_enum_failure_preserve_cached_state(
    db_session, materializer, value, metadata
):
    parent, _, _, _ = _seed_resource_graph(db_session)
    first = materializer.materialize(ResourceSchema, _batch(parent))[0]
    batch = _batch(parent)
    attribute = next(
        record for record in batch.entities if record.kind == "attribute_template"
    )
    attribute.fields["metadata"] = metadata
    if "choices" in metadata:
        attribute.fields.update(value_type="enum", default_value="good")
    stored = next(
        record for record in batch.entities if record.kind == "attribute_value"
    )
    stored.fields["value"] = value
    with pytest.raises(ValueError):
        materializer.materialize(ResourceSchema, batch)
    assert first.properties.measurements.dose.value == 1
    assert first.is_loaded("properties")


def test_null_required_template_rejected_before_cached_mutation(
    db_session, materializer
):
    parent, _, _, _ = _seed_resource_graph(db_session)
    first = materializer.materialize(ResourceSchema, _batch(parent))[0]
    properties, template = first.properties, first.template
    batch = _batch(parent)
    record = next(
        record
        for record in batch.entities
        if record.kind == "resource" and record.id == parent.id
    )
    record.fields["name"] = "must not apply"
    edge = next(
        edge
        for edge in batch.adjacency
        if edge.source.id == parent.id and edge.relation == "template"
    )
    edge.target = None
    with pytest.raises(RecapProtocolError, match="required.*template"):
        materializer.materialize(ResourceSchema, batch)
    assert first.name == "parent"
    assert first.template is template
    assert first.properties is properties


def test_omitted_metadata_uses_default_on_first_value_load(db_session, materializer):
    parent, _, _, _ = _seed_resource_graph(db_session)
    batch = _batch(parent)
    record = next(
        record for record in batch.entities if record.kind == "attribute_value"
    )
    del record.fields["metadata_json"]
    result = materializer.materialize(ResourceSchema, batch)[0]
    assert result.properties.measurements.dose.metadata_json == {}


def test_stale_run_does_not_mutate_owned_steps_or_parameters(db_session, materializer):
    run = _seed_run_with_resource_chain(
        db_session, name="stale-materialization", depth=1
    )
    collector = EntityBatchCollector()
    collector.add_root(run, {"template", "steps", "steps.parameters", "resources"})
    batch = collector.collect()
    first = materializer.materialize(ProcessRunSchema, batch)[0]
    step = next(iter(first.steps.values()))
    group = next(iter(step.parameters.values()))
    run_record = next(
        record for record in batch.entities if record.kind == "process_run"
    )
    run_record.revision -= 1
    step_record = next(record for record in batch.entities if record.kind == "step")
    step_record.fields["name"] = "stale name"
    value_record = next(
        record for record in batch.entities if record.kind == "attribute_value"
    )
    value_record.fields["value"] = 99
    materializer.materialize(ProcessRunSchema, batch)
    assert step.name != "stale name"
    assert group.values.dwell.value == 1
    assert next(iter(first.steps.values())) is step


def test_group_alias_change_replaces_generated_class(db_session, materializer):
    parent, _, _, _ = _seed_resource_graph(db_session)
    batch = _batch(parent)
    value = next(
        record for record in batch.entities if record.kind == "attribute_value"
    )
    value.fields["unit"] = "mL"
    first = materializer.materialize(ResourceSchema, batch)[0]
    original = first.properties
    template = next(
        record for record in batch.entities if record.kind == "attribute_group_template"
    )
    template.fields["name"] = "Renamed Measurements"
    materializer.materialize(ResourceSchema, batch)
    assert first.properties is not original
    assert (
        type(first.properties).model_fields["measurements"].alias
        == "Renamed Measurements"
    )
    assert first.properties["Renamed Measurements"].dose.value == 1
    del value.fields["unit"]
    materializer.materialize(ResourceSchema, batch)
    assert first.properties["Renamed Measurements"].dose.unit == "mL"


def test_value_refresh_preserves_omitted_metadata_and_unit(db_session, materializer):
    parent, _, _, _ = _seed_resource_graph(db_session)
    batch = _batch(parent)
    value = next(
        record for record in batch.entities if record.kind == "attribute_value"
    )
    value.fields.update(unit="mL", metadata_json={"source": "scan"})
    first = materializer.materialize(ResourceSchema, batch)[0]
    value.fields["value"] = 3
    del value.fields["unit"]
    del value.fields["metadata_json"]
    materializer.materialize(ResourceSchema, batch)
    dose = first.properties.measurements.dose
    assert dose.value == 3
    assert dose.unit == "mL"
    assert dose.metadata_json == {"source": "scan"}


def test_concurrent_materialization_returns_one_canonical_graph(
    db_session, materializer
):
    from concurrent.futures import ThreadPoolExecutor

    parent, _, _, _ = _seed_resource_graph(db_session)
    batch = _batch(parent)
    with ThreadPoolExecutor(max_workers=8) as executor:
        models = list(
            executor.map(
                lambda _index: materializer.materialize(ResourceSchema, batch)[0],
                range(32),
            )
        )
    assert all(model is models[0] for model in models)
    assert all(model.properties is models[0].properties for model in models)
    assert all(model.children["child"].parent is models[0] for model in models)


@pytest.mark.performance
@pytest.mark.parametrize(
    "client_fixture", ["client", "rest_loopback_client"], ids=["local", "rest"]
)
def test_client_batch_reads_reuse_canonical_identity(request, client_fixture):
    client = request.getfixturevalue(client_fixture)
    with client.build_resource_template(name="BatchPath", type_names=["sample"]):
        pass
    resource = client.create_resource("sample", "BatchPath")
    with client.build_resource(resource_id=resource.id) as builder:
        builder.finalize()
    first = client.query_maker().resources(load="eager").filter(id=resource.id).first()
    second = client.query_maker().resources(load="eager").filter(id=resource.id).first()
    assert first is second
