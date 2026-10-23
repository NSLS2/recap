from unittest.mock import patch
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import sessionmaker

from recap.adapter.entity_batch import (
    EntityBatch,
    EntityBatchCollector,
    EntityRef,
    NamedAdjacency,
    SequenceAdjacency,
    SingleAdjacency,
)
from recap.adapter.local import LocalBackend
from recap.adapter.schema_registry import SCHEMA_REGISTRY
from recap.authorization.query import AuthorizedQuery
from recap.authorization.scopes import Scope
from recap.client.permissions import EffectivePermissions
from recap.db.process import allow_in_place_edit
from recap.db.step import Step
from recap.dsl.query import QuerySpec
from recap.exceptions import RecapNotFoundError
from recap.schemas.process import (
    ProcessRunSchema,
    ProcessTemplateRef,
    ProcessTemplateSchema,
)
from recap.schemas.resource import (
    ResourceRef,
    ResourceSchema,
    ResourceTemplateRef,
    ResourceTemplateSchema,
)

from .conftest import count_statements
from .entity_fixtures import _seed_process_template, _seed_resource_graph
from .test_process_run_resource_tree_perf import _seed_run_with_resource_chain


def test_adjacency_contract_preserves_null_named_order_and_completeness():
    source = EntityRef(kind="resource", id=uuid4())
    first = EntityRef(kind="resource", id=uuid4())
    second = EntityRef(kind="resource", id=uuid4())
    edges = [
        SingleAdjacency(source=source, relation="parent", target=None),
        NamedAdjacency(
            source=source, relation="children", targets={"well": first}, complete=False
        ),
        SequenceAdjacency(source=source, relation="children", targets=[second, first]),
    ]
    batch = EntityBatch(roots=[source], entities=[], adjacency=edges)
    parsed = EntityBatch.model_validate_json(batch.model_dump_json())
    assert parsed.adjacency[0].target is None
    assert not parsed.adjacency[1].complete
    assert parsed.adjacency[2].targets == [second, first]
    with pytest.raises(ValidationError):
        EntityRef(kind="unknown", id=uuid4())


def test_collector_merges_requested_coverage_and_preserves_root_order(db_session):
    parent, child, _, _ = _seed_resource_graph(db_session)
    collector = EntityBatchCollector()
    collector.add_root(parent, {"children"})
    collector.add_root(child, {"properties"})
    collector.add_root(parent, {"template"})
    batch = collector.collect()
    resources = [record for record in batch.entities if record.kind == "resource"]
    assert len(resources) == 2
    assert [ref.id for ref in batch.roots] == [parent.id, child.id, parent.id]
    edges = {(edge.source.id, edge.relation): edge for edge in batch.adjacency}
    assert edges[parent.id, "children"].targets["child"].id == child.id
    assert edges[parent.id, "template"].loaded
    assert edges[child.id, "properties"].loaded
    keys = {(record.kind, record.id) for record in batch.entities}
    assert len(keys) == len(batch.entities)
    for edge in batch.adjacency:
        assert (edge.source.kind, edge.source.id) in keys
        targets = edge.references()
        assert all((target.kind, target.id) in keys for target in targets)


def test_nonrevisioned_records_preserve_metadata_and_stored_value_rows(db_session):
    parent, _, _, _ = _seed_resource_graph(db_session)
    stored = parent.properties["Measurements"]._values["dose"]
    stored.value_json = None
    stored.unit = "mL"
    stored.metadata_json = {"source": ["scan", "scan"]}
    collector = EntityBatchCollector()
    collector.add_root(parent, {"properties"})
    batch = collector.collect()
    values = [record for record in batch.entities if record.kind == "attribute_value"]
    assert len(values) == 1
    assert values[0].id == stored.id
    assert values[0].fields["value"] is None
    assert values[0].fields["metadata_json"] == {"source": ["scan", "scan"]}
    assert values[0].fields["unit"] == "mL"
    for record in batch.entities:
        if record.kind not in {
            "resource",
            "resource_template",
            "process_template",
            "process_run",
            "namespace",
        }:
            assert "revision" not in record.model_dump()
    parsed = EntityBatch.model_validate_json(batch.model_dump_json())
    assert parsed.model_dump(mode="json") == batch.model_dump(mode="json")


def test_flat_children_do_not_access_orm_child_collections(db_session):
    parent, child, _, _ = _seed_resource_graph(db_session)
    collector = EntityBatchCollector(children_map={parent.id: [child]})
    collector.add_root(parent, {"children"})
    batch = collector.collect()
    edges = [
        edge
        for edge in batch.adjacency
        if edge.relation == "children" and edge.source.kind == "resource"
    ]
    assert len(edges) == 2
    assert next(edge for edge in edges if edge.source.id == child.id).targets == {}


def test_leaf_projection_does_not_read_unrequested_relationships(db_session):
    _, _, template, _ = _seed_resource_graph(db_session)
    collector = EntityBatchCollector()
    collector.add_root(template, set())
    batch = collector.collect()
    assert len(batch.entities) == 1
    assert batch.adjacency == []
    assert batch.entities[0].fields["name"] == template.name


def test_process_template_supporting_records_are_not_public_graphs(db_session):
    template, step = _seed_process_template(db_session)
    collector = EntityBatchCollector()
    collector.add_root(template, {"step_templates", "resource_slots"})
    batch = collector.collect()
    assert {record.kind for record in batch.entities} >= {
        "process_template",
        "step_template",
        "attribute_group_template",
        "attribute_template",
    }
    assert (
        next(edge for edge in batch.adjacency if edge.relation == "step_templates")
        .targets["Collect"]
        .id
        == step.id
    )


def test_unknown_relationship_is_rejected(db_session):
    parent, _, _, _ = _seed_resource_graph(db_session)
    collector = EntityBatchCollector()
    with pytest.raises(ValueError, match="Unsupported"):
        collector.add_root(parent, {"not_a_relation"})


def test_local_batch_query_is_direct_and_preserves_requested_scope(db_session):
    parent, child, _, _ = _seed_resource_graph(db_session)
    db_session.commit()
    backend = LocalBackend(sessionmaker(bind=db_session.get_bind()))
    batch = backend.query_batch(
        ResourceSchema,
        QuerySpec(
            filters={"id": parent.id}, preloads=("children",), include_mutable=True
        ),
        namespace_path=parent.namespace.path,
    )
    assert batch.roots == [EntityRef(kind="resource", id=parent.id)]
    assert {record.id for record in batch.entities if record.kind == "resource"} == {
        parent.id,
        child.id,
    }
    assert not any(edge.relation == "properties" for edge in batch.adjacency)


@pytest.mark.parametrize("authorized", [False, True])
def test_direct_local_query_materializes_batch_entry_point(db_session, authorized):
    parent, child, _, _ = _seed_resource_graph(db_session)
    parent_id, child_id, path = parent.id, child.id, parent.namespace.path
    db_session.commit()
    backend = LocalBackend(sessionmaker(bind=db_session.get_bind()))
    spec = QuerySpec(filters={"id": parent_id}, load_mode="eager", include_mutable=True)
    authorization = AuthorizedQuery(
        namespace_path=path,
        permissions=EffectivePermissions(
            identities=(),
            snapshot_generation=None,
            effective_scopes=frozenset({Scope.RESOURCE_READ, Scope.RESOURCE_WRITE}),
            matched_namespace_paths=(path,),
            grants=(),
        ),
    )
    entry = "query_batch_authorized" if authorized else "query_batch"
    with patch.object(backend, entry, wraps=getattr(backend, entry)) as collect:
        if authorized:
            models = backend.query_authorized(
                ResourceSchema, spec, authorization=authorization
            )
        else:
            models = backend.query(ResourceSchema, spec, namespace_path=path)
    collect.assert_called_once()
    assert models[0].children["child"].id == child_id
    assert models[0].properties.measurements.values.dose.value == 1
    assert models[0].is_loaded("children")
    again = backend.query(ResourceSchema, spec, namespace_path=path)[0]
    assert again is not models[0]


@pytest.mark.parametrize("expand", [False, True])
@pytest.mark.parametrize("kind", ["resource", "resource_template", "process_template"])
def test_direct_getters_collect_records_and_preserve_return_types(
    db_session, kind, expand
):
    parent, _, template, _ = _seed_resource_graph(db_session)
    process_template, _ = _seed_process_template(db_session)
    namespace_id, name, template_name, version = (
        parent.namespace_id,
        parent.name,
        template.name,
        template.version,
    )
    template_id = template.id
    process_namespace, process_id = process_template.namespace_id, process_template.id
    process_version = process_template.version
    db_session.commit()
    backend = LocalBackend(sessionmaker(bind=db_session.get_bind()))
    with patch.object(
        EntityBatchCollector,
        "collect",
        autospec=True,
        side_effect=EntityBatchCollector.collect,
    ) as collect:
        if kind == "resource":
            result = backend.get_resource(
                namespace_id, name, template_name, version, expand=expand
            )
            expected = ResourceSchema if expand else ResourceRef
        elif kind == "resource_template":
            result = backend.get_resource_template(
                namespace_id, None, id=str(template_id), expand=expand
            )
            expected = ResourceTemplateSchema if expand else ResourceTemplateRef
        else:
            result = backend.get_process_template(
                process_namespace,
                None,
                process_version,
                id=str(process_id),
                expand=expand,
            )
            expected = ProcessTemplateSchema if expand else ProcessTemplateRef
    collect.assert_called_once()
    assert type(result) is expected
    if expand and kind == "resource":
        assert result.properties.measurements.values.dose.value == 1
        assert "child" in result.children
    if expand and kind == "process_template":
        assert "Collect" in result.step_templates


def test_direct_step_and_parameter_getters_materialize_records(db_session):
    run = _seed_run_with_resource_chain(db_session, name="direct-getters", depth=2)
    run_id, path = run.id, run.namespace.path
    backend = LocalBackend(sessionmaker(bind=db_session.get_bind()))
    model = backend.query_batch(
        ProcessRunSchema, QuerySpec(filters={"id": run_id}), namespace_path=path
    )
    from recap.client.identity import IdentityMap
    from recap.client.materialization import BatchMaterializer

    run_schema = BatchMaterializer(IdentityMap()).materialize(ProcessRunSchema, model)[
        0
    ]
    with patch.object(
        EntityBatchCollector,
        "collect",
        autospec=True,
        side_effect=EntityBatchCollector.collect,
    ) as collect:
        steps = backend.get_steps(run_schema)
    collect.assert_called_once()
    assert len(steps) == 1
    step = steps[0]
    assert step.resources["input"].name == "direct-getters-0"
    assert (
        next(iter(type(step.parameters).model_fields.values())).alias
        == "Parameters-direct-getters"
    )
    with patch.object(
        EntityBatchCollector,
        "collect",
        autospec=True,
        side_effect=EntityBatchCollector.collect,
    ) as collect:
        params = backend.get_params(step)
    collect.assert_called_once()
    assert params.step_id == step.id
    assert params.step_name == step.name
    group = next(
        value
        for key, value in params.__dict__.items()
        if key not in {"step_id", "step_name"}
    )
    assert group.dwell.value == 1
    missing_step = step.model_copy(update={"id": uuid4()})
    with pytest.raises(RecapNotFoundError, match="Step not found"):
        backend.get_params(missing_step)


def test_direct_getters_preserve_selection_errors_and_parent_filter(db_session):
    parent, child, template, child_template = _seed_resource_graph(db_session)
    namespace_id, template_id, child_id = (
        parent.namespace_id,
        template.id,
        child_template.id,
    )
    version, name = template.version, template.name
    db_session.commit()
    backend = LocalBackend(sessionmaker(bind=db_session.get_bind()))
    parent_ref = backend.get_resource_template(namespace_id, None, id=template_id)
    assert (
        backend.get_resource_template(namespace_id, "child", parent=parent_ref).id
        == child_id
    )
    with pytest.raises(LookupError, match="no rows matched"):
        backend.get_resource_template(namespace_id, name, version="missing")
    with pytest.raises(LookupError, match="no rows matched"):
        backend.get_resource(namespace_id, "missing", name, version)
    with pytest.raises(ValueError, match="name or id required"):
        backend.get_resource_template(namespace_id, None)
    # ORM identity lookup remains a database/domain operation.
    found = backend.find_resources_by_identity(
        namespace_id, parent.name, None, template_id
    )
    from recap.db.resource import Resource

    assert len(found) == 1
    assert isinstance(found[0], Resource)
    with pytest.raises(LookupError, match="no rows matched"):
        backend.get_resource_template(
            namespace_id, "child", parent=parent_ref.model_copy(update={"id": uuid4()})
        )


def test_template_child_backreferences_do_not_expand_root_load_state(db_session):
    parent, _, template, _ = _seed_resource_graph(db_session)
    template_id, path = template.id, parent.namespace.path
    db_session.commit()
    backend = LocalBackend(sessionmaker(bind=db_session.get_bind()))
    batch = backend.query_batch(
        ResourceTemplateSchema,
        QuerySpec(filters={"id": template_id}, preloads=("children",)),
        namespace_path=path,
    )
    root_parent = next(
        edge
        for edge in batch.adjacency
        if edge.source.id == template_id and edge.relation == "parent"
    )
    assert not root_parent.loaded


def test_registry_exposes_matching_record_policies():
    for key in list(SCHEMA_REGISTRY.keys()):
        registration = SCHEMA_REGISTRY.by_key(key)
        assert registration.record_policy.orm is registration.orm_model
        assert registration.record_policy.schema is registration.model


@pytest.mark.performance
@pytest.mark.parametrize("model_results", [False, True])
def test_batch_resource_subtrees_have_depth_independent_sql_budget(
    db_session, model_results
):
    run3 = _seed_run_with_resource_chain(db_session, name="batch-three", depth=3)
    run4 = _seed_run_with_resource_chain(db_session, name="batch-four", depth=4)
    backend = LocalBackend(sessionmaker(bind=db_session.get_bind()))
    counts = []
    for run, depth in ((run3, 3), (run4, 4)):
        run_id, path = run.id, run.namespace.path
        with count_statements(db_session.get_bind()) as counter:
            result = (backend.query if model_results else backend.query_batch)(
                ProcessRunSchema,
                QuerySpec(
                    filters={"id": run_id}, preloads=("resources", "steps.parameters")
                ),
                namespace_path=path,
            )
        counts.append(counter["n"])
        if model_results:
            assert len(result) == 1
            from .test_process_run_resource_tree_perf import _walk_depth

            assert (
                _walk_depth(next(iter(result[0].assigned_resources.values())).resource)
                == depth
            )
            continue
        batch = result
        assert (
            len([record for record in batch.entities if record.kind == "resource"])
            == depth
        )
        assert any(record.kind == "resource_assignment" for record in batch.entities)
        assert any(record.kind == "parameter" for record in batch.entities)
        assert any(edge.relation == "resources" for edge in batch.adjacency)
        keys = {(record.kind, record.id) for record in batch.entities}
        assert all(
            (target.kind, target.id) in keys
            for edge in batch.adjacency
            for target in edge.references()
        )
    assert abs(counts[0] - counts[1]) <= 1
    assert max(counts) <= 45


@pytest.mark.parametrize(
    "schema_key", ["namespace", "resource_template", "process_template"]
)
def test_local_batch_queries_cover_all_registered_root_kinds(db_session, schema_key):
    parent, _, resource_template, _ = _seed_resource_graph(db_session)
    process_template, _ = _seed_process_template(db_session)
    rows = {
        "namespace": parent.namespace,
        "resource_template": resource_template,
        "process_template": process_template,
    }
    row = rows[schema_key]
    path = row.path if schema_key == "namespace" else row.namespace.path
    db_session.commit()
    registration = SCHEMA_REGISTRY.by_key(schema_key)
    backend = LocalBackend(sessionmaker(bind=db_session.get_bind()))
    spec = QuerySpec(filters={"id": row.id}, include_mutable=True, load_mode="eager")
    batch = backend.query_batch(registration.model, spec, namespace_path=path)
    assert batch.roots == [EntityRef(kind=schema_key, id=row.id)]
    record = next(
        record
        for record in batch.entities
        if record.kind == schema_key and record.id == row.id
    )
    assert record.revision == row.revision
    if schema_key == "namespace":
        assert record.fields["metadata"] == row.metadata_json
    assert batch.entities


@pytest.mark.performance
def test_batch_resource_sql_matches_existing_tree_load(db_session):
    parent, child, _, _ = _seed_resource_graph(db_session)
    path, parent_id = parent.namespace.path, parent.id
    db_session.commit()
    backend = LocalBackend(sessionmaker(bind=db_session.get_bind()))
    spec = QuerySpec(filters={"id": parent_id}, load_mode="eager", include_mutable=True)
    with count_statements(db_session.get_bind()) as old_counter:
        models = backend.query(ResourceSchema, spec, namespace_path=path)
    with count_statements(db_session.get_bind()) as batch_counter:
        batch = backend.query_batch(ResourceSchema, spec, namespace_path=path)
    assert batch_counter["n"] <= old_counter["n"]
    assert len([record for record in batch.entities if record.kind == "resource"]) == 2
    assert models[0].children["child"].id == child.id


def test_process_batch_preserves_runtime_child_and_resource_override_views(db_session):
    run = _seed_run_with_resource_chain(db_session, name="batch-runtime", depth=2)
    root_step = next(iter(run.steps.values()))
    allow_in_place_edit(db_session, run)
    with db_session.no_autoflush:
        child = Step(
            name="repeat",
            template=root_step.template,
            parent=root_step,
            process_run=run,
        )
        db_session.add(child)
    db_session.flush()
    run_id, child_id, root_id, path = run.id, child.id, root_step.id, run.namespace.path
    db_session.commit()
    backend = LocalBackend(sessionmaker(bind=db_session.get_bind()))
    batch = backend.query_batch(
        ProcessRunSchema,
        QuerySpec(filters={"id": run_id}, load_mode="eager"),
        namespace_path=path,
    )
    edges = {(edge.source.id, edge.relation): edge for edge in batch.adjacency}
    assert edges[root_id, "children"].targets["repeat"].id == child_id
    assert child_id not in {
        target.id for target in edges[run_id, "steps"].targets.values()
    }
    assert edges[child_id, "resource_overrides"].targets == {}
    assert "input" in edges[child_id, "resources"].targets
    assert edges[child_id, "template"].loaded
