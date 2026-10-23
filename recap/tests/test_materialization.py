from unittest.mock import patch

import pytest
from pydantic import ValidationError

from recap.adapter.entity_batch import EntityBatchCollector
from recap.adapter.entity_records import EntityRef, SingleAdjacency
from recap.client.identity import IdentityMap
from recap.client.materialization import BatchMaterializer
from recap.exceptions import RecapProtocolError
from recap.schemas.resource import ResourceSchema

from .test_hydration_cache import _seed_resource_graph


def _batch(parent, *, requested=None):
    collector = EntityBatchCollector()
    collector.add_root(
        parent, requested or {"template", "parent", "children", "properties"}
    )
    from sqlalchemy.orm import object_session

    with object_session(parent).no_autoflush:
        return collector.collect()


def test_batch_materialization_preserves_cycles_identity_and_typed_values(db_session):
    parent, child, _, _ = _seed_resource_graph(db_session)
    identities = IdentityMap()
    materializer = BatchMaterializer(identities)
    first = materializer.materialize(ResourceSchema, _batch(parent))[0]
    second = materializer.materialize(ResourceSchema, _batch(parent))[0]
    assert first is second
    assert first.children["child"].parent is first
    assert first.children["child"].id == child.id
    assert first.properties.measurements.dose.value == 1
    assert first.properties is second.properties
    assert first.properties.measurements.values is second.properties.measurements.values


def test_newer_child_updates_under_unchanged_parent_without_recursive_signatures(
    db_session,
):
    parent, child, _, _ = _seed_resource_graph(db_session)
    materializer = BatchMaterializer(IdentityMap())
    first = materializer.materialize(ResourceSchema, _batch(parent))[0]
    original_child = first.children["child"]
    child.name = "updated"
    child.revision += 1
    with patch.object(
        IdentityMap,
        "_relation_signature",
        side_effect=AssertionError("Graph signature"),
    ):
        second = materializer.materialize(ResourceSchema, _batch(parent))[0]
    assert second is first
    assert original_child.name == "updated"
    assert original_child.revision == child.revision


def test_partial_reads_do_not_downgrade_loaded_data_and_older_reads_are_ignored(
    db_session,
):
    parent, _, _, _ = _seed_resource_graph(db_session)
    materializer = BatchMaterializer(IdentityMap())
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


def test_property_updates_preserve_generated_references_and_null_metadata(db_session):
    parent, _, _, _ = _seed_resource_graph(db_session)
    materializer = BatchMaterializer(IdentityMap())
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


def test_invalid_typed_value_does_not_change_cached_entity(db_session):
    parent, _, _, _ = _seed_resource_graph(db_session)
    identities = IdentityMap()
    materializer = BatchMaterializer(identities)
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


def test_complete_membership_replacement_and_partial_extension(db_session):
    parent, child, _, _ = _seed_resource_graph(db_session)
    materializer = BatchMaterializer(IdentityMap())
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


def test_array_values_preserve_order_repetition_and_detached_draft(db_session):
    parent, _, _, _ = _seed_resource_graph(db_session)
    template = parent.properties["Measurements"].template.attribute_templates[0]
    template.value_type = "array"
    template.default_value = "[]"
    stored = parent.properties["Measurements"]._values["dose"]
    stored.value_json = [1, 2, 1, 2]
    materializer = BatchMaterializer(IdentityMap())
    first = materializer.materialize(ResourceSchema, _batch(parent))[0]
    draft = first.model_copy(deep=True)
    draft.properties.measurements.dose.value.append(3)
    stored.value_json = [4, 4]
    parent.revision += 1
    materializer.materialize(ResourceSchema, _batch(parent))
    assert first.properties.measurements.dose.value == [4, 4]
    assert draft.properties.measurements.dose.value == [1, 2, 1, 2, 3]


@pytest.mark.performance
@pytest.mark.parametrize(
    "client_fixture", ["client", "rest_loopback_client"], ids=["local", "rest"]
)
def test_client_batch_path_does_not_build_recursive_signatures(request, client_fixture):
    client = request.getfixturevalue(client_fixture)
    with client.build_resource_template(name="BatchPath", type_names=["sample"]):
        pass
    resource = client.create_resource("sample", "BatchPath")
    with client.build_resource(resource_id=resource.id) as builder:
        builder.finalize()
    with patch.object(
        IdentityMap,
        "_relation_signature",
        side_effect=AssertionError("Recursive signature"),
    ):
        first = (
            client.query_maker().resources(load="eager").filter(id=resource.id).first()
        )
        second = (
            client.query_maker().resources(load="eager").filter(id=resource.id).first()
        )
    assert first is second
