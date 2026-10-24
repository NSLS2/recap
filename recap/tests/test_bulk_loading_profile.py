"""Cold distinct-template graphs expose preparation and extraction SQL separately."""

import hashlib
from contextlib import contextmanager
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from recap.adapter.entity_batch import EntityBatchCollector
from recap.adapter.entity_loading import prepare_batch_graph
from recap.adapter.local import LocalBackend
from recap.adapter.query_loaders import resolve_loader_options
from recap.client.identity import IdentityMap
from recap.client.materialization import BatchMaterializer
from recap.commands.context import build_local_command_context
from recap.commands.models import CreateResource
from recap.commands.service import CommandService
from recap.db.process import ProcessRun
from recap.db.resource import Resource, ResourceTemplate
from recap.dsl.query import Field, QuerySpec
from recap.schemas.process import ProcessRunSchema
from recap.schemas.resource import ResourceSchema, ResourceTemplateSchema
from scripts.profile_bulk_loading import (
    capture_sql,
    profile_cases,
    profile_read,
    readonly_engine,
)

from .conftest import count_statements
from .entity_fixtures import (
    record_loaded_plate,
    seed_loaded_well_plate,
    setup_plate_workflow,
)


@pytest.fixture(params=[12, 96], ids=["12-wells", "96-wells"])
def loaded_plate(request, blank_database_path, copy_database, tmp_path):
    path = copy_database(blank_database_path, tmp_path / "loaded-plate.db")
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    factory = sessionmaker(bind=engine, autoflush=False)
    try:
        with factory() as session:
            graph = seed_loaded_well_plate(session, request.param)
            session.commit()
        yield factory, graph
    finally:
        engine.dispose()


def test_fixture_has_distinct_templates_and_committed_values(loaded_plate):
    factory, graph = loaded_plate
    with factory() as session:
        plate = session.get(Resource, graph.plate_id)
        assert len(plate.children) == len(graph.well_ids)
        assert len(
            {well.resource_template_id for well in plate.children.values()}
        ) == len(graph.well_ids)
        for well in plate.children.values():
            assert len(well.properties) == 3
            assert sum(len(group._values) for group in well.properties.values()) == 5
            assert well.properties["Measurements"]._values["Mass"].unit == "mg"
            assert well.properties["Measurements"].values["Readings"] == [1, 1, 2]
            assert well.properties["Identity"].values["Note"] is None


def test_statement_attribution_matches_existing_counter(loaded_plate):
    factory, graph = loaded_plate
    with count_statements(factory.kw["bind"]) as counter:
        batch, profile = profile_read(factory, graph.namespace_path, graph.plate_id)
    assert profile.total_sql == counter["n"]
    assert sum(profile.sql_by_phase.values()) == profile.total_sql
    assert profile.loaded_types["Resource"] == len(graph.well_ids) + 1
    assert not profile.lazy_relationships
    assert profile.planned_relationships
    assert profile.callers
    assert batch.roots[0].id == graph.plate_id


@pytest.mark.parametrize("command_phases", [False, True], ids=["reads", "commands"])
def test_capture_ignores_other_engines_and_cleans_up_on_error(
    loaded_plate, command_phases
):
    factory, graph = loaded_plate
    other = create_engine(factory.kw["bind"].url)
    original_snapshot, original_flush = CommandService._graph_result, Session.flush
    try:
        with (
            pytest.raises(RuntimeError),
            capture_sql(factory.kw["bind"], command_phases=command_phases) as profile,
        ):
            with other.connect() as connection:
                connection.exec_driver_sql("SELECT 1")
            with Session(other) as session:
                plate = session.get(Resource, graph.plate_id)
                assert len(plate.children) == len(graph.well_ids)
                assert len(next(iter(plate.children.values())).properties) == 3
            with factory() as session:
                session.scalar(select(Resource).where(Resource.id == graph.plate_id))
            raise RuntimeError("diagnostic failure")
        assert profile.total_sql == 1
        assert profile.loaded_types == {"Resource": 1}
        assert not profile.lazy_relationships
        before = profile.total_sql
        with factory() as session:
            session.get(Resource, graph.plate_id)
        assert profile.total_sql == before
        assert CommandService._graph_result is original_snapshot
        assert Session.flush is original_flush
    finally:
        other.dispose()


READ_CASES = [
    ("plate", "resource", (), False),
    ("well", "resource", (), False),
    ("plate", "resource", ("children",), False),
    ("run", "process_run", (), False),
    ("plate", "resource", (), True),
    ("well", "resource", (), True),
    ("run", "process_run", (), True),
]
CASE_IDS = [
    "plate-eager",
    "well-eager",
    "children-only",
    "run-eager",
    "plate-snapshot",
    "well-snapshot",
    "run-snapshot",
]


def read_graph(factory, graph, root, kind, preloads, snapshot):
    object_id = graph.well_ids[0] if root == "well" else getattr(graph, f"{root}_id")
    return profile_read(
        factory,
        graph.namespace_path,
        object_id,
        kind=kind,
        preloads=preloads,
        snapshot=snapshot,
    )


@pytest.mark.parametrize("root,kind,preloads,snapshot", READ_CASES, ids=CASE_IDS)
def test_cold_batches_preserve_graph_and_include_semantics(
    loaded_plate, root, kind, preloads, snapshot
):
    factory, graph = loaded_plate
    batch, profile = read_graph(factory, graph, root, kind, preloads, snapshot)
    records = {(record.kind, record.id): record for record in batch.entities}
    assert len(records) == len(batch.entities)
    for edge in batch.adjacency:
        assert (edge.source.kind, edge.source.id) in records
        assert all((target.kind, target.id) in records for target in edge.references())
    edges = {
        (edge.source.kind, edge.source.id, edge.relation): edge
        for edge in batch.adjacency
    }
    if preloads:
        assert len(batch.entities) == 2 * (len(graph.well_ids) + 1)
        assert not any(edge.relation == "properties" for edge in batch.adjacency)
        assert {
            target.id
            for target in edges["resource", graph.plate_id, "children"].targets.values()
        } == set(graph.well_ids)
    elif root != "well":
        assert len(edges["resource", graph.plate_id, "children"].targets) == len(
            graph.well_ids
        )
    else:
        # Existing eager semantics include the parent template and sibling definitions.
        templates = [
            record for record in batch.entities if record.kind == "resource_template"
        ]
        assert len(templates) >= (1 if snapshot else len(graph.well_ids) + 1)
    if not preloads:
        assert edges["resource", graph.well_ids[0], "properties"].loaded
        values = [
            record.fields
            for record in batch.entities
            if record.kind == "attribute_value"
        ]
        assert any(value["value"] is None for value in values)
        assert any(value["value"] == [1, 1, 2] for value in values)
        assert any(value["value"] == 2.5 and value["unit"] == "mg" for value in values)
    if kind == "process_run":
        steps = [record for record in batch.entities if record.kind == "step"]
        assert len(steps) == 3
        root_step = next(step for step in steps if step.fields["name"] == "Transfer")
        children = edges["step", root_step.id, "children"].targets
        assert len(children) == 2
        assert {
            edges["step", child.id, "resources"].targets["source"].id
            for child in children.values()
        } == set(graph.source_ids)
        assert {
            edges["step", child.id, "resource_overrides"].targets["destination"].id
            for child in children.values()
        } == set(graph.well_ids[:2])
    if snapshot and kind == "resource":
        assert ("resource", batch.roots[0].id, "parent") not in edges
    schema = ResourceSchema if kind == "resource" else ProcessRunSchema
    with count_statements(factory.kw["bind"]) as count:
        models = BatchMaterializer(IdentityMap()).materialize(schema, batch)
    assert count["n"] == 0
    assert models[0].id == batch.roots[0].id
    if preloads:
        assert models[0].is_loaded("children")
        assert not models[0].is_loaded("properties")
    assert sum(profile.sql_by_phase.values()) == profile.total_sql


def test_profile_cases_repeat_with_fresh_sessions(loaded_plate):
    factory, graph = loaded_plate
    args = (
        factory,
        graph.namespace_path,
        graph.plate_id,
        graph.well_ids[0],
        graph.run_id,
    )
    first, second = profile_cases(*args), profile_cases(*args)
    assert len(first) == 7
    for previous, repeated in zip(first, second, strict=True):
        assert {key: value for key, value in previous.items() if key != "seconds"} == {
            key: value for key, value in repeated.items() if key != "seconds"
        }


def test_expired_columns_are_not_relationship_loads(loaded_plate):
    factory, graph = loaded_plate
    with factory() as session:
        plate = session.get(Resource, graph.plate_id)
        session.expire(plate, ["name"])
        with capture_sql(factory.kw["bind"]) as profile:
            assert plate.name == "Loaded plate"
    assert profile.total_sql == profile.column_refreshes == 1
    assert not profile.lazy_relationships
    assert not profile.planned_relationships


def test_plain_relationship_access_is_classified_as_lazy(loaded_plate):
    factory, graph = loaded_plate
    with factory() as session:
        plate = session.get(Resource, graph.plate_id)
        with capture_sql(factory.kw["bind"]) as profile:
            assert len(plate.children) == len(graph.well_ids)
    assert profile.total_sql == sum(profile.lazy_relationships.values()) == 1
    assert not profile.planned_relationships
    assert profile.loaded_types["Resource"] == len(graph.well_ids)


def test_resources_include_loads_assignment_keys_with_rows(loaded_plate):
    factory, graph = loaded_plate
    with factory() as session, capture_sql(factory.kw["bind"]) as profile:
        run = session.scalar(
            select(ProcessRun)
            .where(ProcessRun.id == graph.run_id)
            .options(*resolve_loader_options(ProcessRunSchema, ["resources"], None))
        )
        assert {slot.name for slot in run.assignments} == {"group", "plate"}
        assert (
            next(
                assignment.resource.id
                for slot, assignment in run.assignments.items()
                if slot.name == "plate"
            )
            == graph.plate_id
        )
    assert profile.loaded_types["ResourceAssignment"] == 2
    assert not profile.lazy_relationships, profile.lazy_relationships


def test_collector_patch_restored_after_collection_exception(loaded_plate):
    factory, graph = loaded_plate
    original = EntityBatchCollector.collect
    with factory() as session:
        row = session.get(Resource, graph.plate_id)
        with capture_sql(factory.kw["bind"]) as profile:
            collector = EntityBatchCollector()
            collector.add_root(row, {"children"})
            with (
                patch.object(collector, "_record", side_effect=RuntimeError("extract")),
                pytest.raises(RuntimeError, match="extract"),
            ):
                collector.collect()
            assert profile.phase == "preparation"
    assert EntityBatchCollector.collect is original


def test_readonly_database_rejects_writes_and_keeps_contents(loaded_plate):
    factory, graph = loaded_plate
    path = factory.kw["bind"].url.database
    with open(path, "rb") as stream:
        before = hashlib.file_digest(stream, "sha256").hexdigest()
    engine = readonly_engine(path)
    try:
        batch, _ = profile_read(
            sessionmaker(bind=engine, autoflush=False),
            graph.namespace_path,
            graph.plate_id,
        )
        assert batch.roots[0].id == graph.plate_id
        with (
            engine.connect() as connection,
            pytest.raises(OperationalError, match="readonly"),
        ):
            connection.exec_driver_sql("CREATE TABLE diagnostic_write (id INTEGER)")
    finally:
        engine.dispose()
    with open(path, "rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == before


@pytest.mark.parametrize("root", ["plate", "run"])
@pytest.mark.performance
def test_collection_keys_do_not_trigger_lazy_loads(loaded_plate, root):
    factory, graph = loaded_plate
    kind = "process_run" if root == "run" else "resource"
    _, profile = read_graph(factory, graph, root, kind, (), False)
    assert not any(
        count
        for path, count in profile.lazy_relationships.items()
        if any(
            f"{owner}.template" in path
            for owner in ("Property", "Parameter", "AttributeValue")
        )
        or "ResourceAssignment.resource_slot" in path
    ), profile.lazy_relationships
    group_batches = sum(
        count
        for path, count in profile.planned_relationships.items()
        if "Property.template" in path
        and "AttributeGroupTemplate.attribute_templates" in path
    )
    # These widths fit one select-in batch; runs also prepare assigned roots.
    assert 1 <= group_batches <= 2, profile.planned_relationships


@pytest.mark.performance
def test_run_preparation_keeps_assigned_tree_loading_within_budget(loaded_plate):
    factory, graph = loaded_plate
    _, profile = profile_read(
        factory, graph.namespace_path, graph.run_id, kind="process_run"
    )
    assert profile.total_sql <= 45, profile.report()


@pytest.mark.performance
def test_children_only_does_not_load_unused_values(loaded_plate):
    factory, graph = loaded_plate
    _, profile = read_graph(factory, graph, "plate", "resource", ("children",), False)
    assert profile.loaded_types["Property"] == 0
    assert profile.loaded_types["AttributeValue"] == 0


@pytest.mark.performance
def test_well_eager_collection_issues_no_sql(loaded_plate):
    factory, graph = loaded_plate
    _, profile = read_graph(factory, graph, "well", "resource", (), False)
    assert profile.sql_by_phase["collection"] == 0


@pytest.mark.parametrize("root_kind", ["resource", "resource_template"])
def test_expanded_direct_getters_prepare_distinct_template_closure(
    loaded_plate, root_kind
):
    factory, graph = loaded_plate
    with factory() as session:
        well = session.get(Resource, graph.well_ids[0])
        namespace_id = well.namespace_id
        name, template_name = well.name, well.template.name
        template_id = well.resource_template_id
    backend = LocalBackend(factory)
    with capture_sql(factory.kw["bind"]) as profile:
        if root_kind == "resource":
            result = backend.get_resource(
                namespace_id, name, template_name, expand=True
            )
            assert result.id == graph.well_ids[0]
            assert result.parent.id == graph.plate_id
        else:
            result = backend.get_resource_template(
                namespace_id, None, id=template_id, expand=True
            )
            assert result.id == template_id
    assert profile.sql_by_phase["collection"] == 0
    assert not profile.lazy_relationships, profile.lazy_relationships


@pytest.mark.parametrize(
    "preloads", [(), ("types",), ("children",), ("attribute_group_templates",)]
)
def test_template_query_preserves_requested_root_coverage_without_collector_sql(
    loaded_plate, preloads
):
    factory, graph = loaded_plate
    with factory() as session:
        plate = session.get(Resource, graph.plate_id)
        template_id = plate.resource_template_id
    with capture_sql(factory.kw["bind"]) as profile:
        batch = LocalBackend(factory).query_batch(
            ResourceTemplateSchema,
            QuerySpec(
                filters={"id": template_id}, preloads=preloads, include_mutable=True
            ),
            namespace_path=graph.namespace_path,
        )
    assert batch.roots[0].id == template_id
    edges = [edge for edge in batch.adjacency if edge.source == batch.roots[0]]
    assert {edge.relation for edge in edges if edge.loaded} == set(preloads) | (
        {"types"} if "children" in preloads else set()
    )
    assert profile.sql_by_phase["collection"] == 0


def test_filtered_paginated_children_queries_keep_selected_root_order(loaded_plate):
    factory, graph = loaded_plate
    with capture_sql(factory.kw["bind"]) as profile:
        batch = LocalBackend(factory).query_batch(
            ResourceSchema,
            QuerySpec(
                filters={"parent_id": graph.plate_id},
                orderings=(Field("name").asc(),),
                offset=1,
                limit=2,
                preloads=("children",),
                include_mutable=True,
            ),
            namespace_path=graph.namespace_path,
        )
    with factory() as session:
        expected = list(
            session.scalars(
                select(Resource.id)
                .where(Resource.parent_id == graph.plate_id)
                .order_by(Resource.name)
                .offset(1)
                .limit(2)
            )
        )
    assert [root.id for root in batch.roots] == expected
    assert (
        profile.loaded_types["Property"] == profile.loaded_types["AttributeValue"] == 0
    )
    assert profile.sql_by_phase["collection"] == 0


def test_direct_step_and_parameter_getters_share_prepared_collection(loaded_plate):
    factory, graph = loaded_plate
    backend = LocalBackend(factory)
    run = backend.query(
        ProcessRunSchema,
        QuerySpec(filters={"id": graph.run_id}, include_mutable=True),
        namespace_path=graph.namespace_path,
    )[0]
    with capture_sql(factory.kw["bind"]) as steps_profile:
        steps = backend.get_steps(run)
    assert len(steps) == 3
    assert steps_profile.sql_by_phase["collection"] == 0
    with capture_sql(factory.kw["bind"]) as params_profile:
        backend.get_params(steps[0])
    assert params_profile.sql_by_phase["collection"] == 0


@pytest.mark.performance
def test_children_only_crosses_row_batch_boundary_without_loading_values(loaded_plate):
    factory, graph = loaded_plate
    with factory() as session:
        plate = session.get(Resource, graph.plate_id)
        template = next(iter(plate.children.values())).template
        for index in range(520 - len(plate.children)):
            session.add(
                Resource(
                    name=f"overflow-{index}",
                    template=template,
                    parent=plate,
                    namespace=plate.namespace,
                    _init_children=False,
                )
            )
        session.commit()
    batch, profile = profile_read(
        factory, graph.namespace_path, graph.plate_id, preloads=("children",)
    )
    assert sum(record.kind == "resource" for record in batch.entities) == 521
    assert (
        profile.loaded_types["Property"] == profile.loaded_types["AttributeValue"] == 0
    )
    assert not profile.lazy_relationships
    assert profile.sql_by_phase["collection"] == 0
    # Two row batches beyond SQLAlchemy's select-in batch size are intentional.
    assert profile.total_sql <= 6


@pytest.mark.parametrize("kind", ["resource", "resource_template", "process_run"])
def test_prepared_graph_matches_original_relation_traversal(loaded_plate, kind):
    factory, graph = loaded_plate
    if kind == "resource":
        model, schema, object_id = Resource, ResourceSchema, graph.well_ids[0]
        requested = {"template", "parent", "children", "properties"}
    elif kind == "resource_template":
        model, schema = ResourceTemplate, ResourceTemplateSchema
        with factory() as session:
            object_id = session.get(Resource, graph.plate_id).resource_template_id
        requested = {"parent", "types", "children", "attribute_group_templates"}
    else:
        model, schema, object_id = ProcessRun, ProcessRunSchema, graph.run_id
        requested = {"template", "steps", "steps.parameters", "resources"}
    with factory() as session:
        original = EntityBatchCollector()
        original.add_root(session.get(model, object_id), requested)
        expected = original.collect()
    with factory() as session, capture_sql(factory.kw["bind"]) as profile:
        actual = prepare_batch_graph(
            session, [session.get(model, object_id)], schema, requested
        )
    assert actual.roots == expected.roots
    assert {
        (record.kind, record.id): record.model_dump(mode="json")
        for record in actual.entities
    } == {
        (record.kind, record.id): record.model_dump(mode="json")
        for record in expected.entities
    }
    assert {
        (edge.source, edge.relation): edge.model_dump(mode="json")
        for edge in actual.adjacency
    } == {
        (edge.source, edge.relation): edge.model_dump(mode="json")
        for edge in expected.adjacency
    }
    assert not profile.lazy_relationships, profile.lazy_relationships
    assert profile.sql_by_phase["collection"] == 0


@pytest.mark.parametrize("root", ["plate", "well", "run"])
@pytest.mark.performance
def test_snapshot_collection_issues_no_sql(loaded_plate, root):
    factory, graph = loaded_plate
    kind = "process_run" if root == "run" else "resource"
    _, profile = read_graph(factory, graph, root, kind, (), True)
    assert profile.sql_by_phase["collection"] == 0
    assert profile.total_sql <= (50 if root == "run" else 20), profile.report()


@pytest.mark.parametrize("root", ["plate", "well", "run"])
def test_expired_snapshots_preserve_updated_scalars_values_and_units(
    loaded_plate, root
):
    factory, graph = loaded_plate
    model = ProcessRun if root == "run" else Resource
    schema = ProcessRunSchema if root == "run" else ResourceSchema
    object_id = graph.well_ids[0] if root == "well" else getattr(graph, f"{root}_id")
    with factory() as session:
        row = session.get(model, object_id)
        CommandService._graph_result(row, schema)
        value = (
            session.get(Resource, graph.well_ids[0])
            .properties["Measurements"]
            ._values["Mass"]
        )
        value.value = 13.5
        value.unit = "g"
        value.metadata_json = {"source": ["updated", "updated"]}
        if root == "run":
            row.description = "Updated before expiry"
        else:
            row.name = "Updated before expiry"
        row.revision += 1
        session.flush()
        revision, modified_date = row.revision, row.modified_date
        with capture_sql(factory.kw["bind"]) as profile:
            session.expire_all()
            batch = CommandService._graph_result(row, schema)
        record = next(
            record
            for record in batch.entities
            if record.id == object_id and record.kind == batch.roots[0].kind
        )
        assert record.revision == revision
        assert record.fields["modified_date"] == modified_date
        assert (
            record.fields["description" if root == "run" else "name"]
            == "Updated before expiry"
        )
        assert any(
            record.kind == "attribute_value"
            and record.fields["value"] == 13.5
            and record.fields["unit"] == "g"
            and record.fields["metadata_json"] == {"source": ["updated", "updated"]}
            for record in batch.entities
        )
        if root != "run":
            assert not any(
                edge.source == batch.roots[0] and edge.relation == "parent"
                for edge in batch.adjacency
            )
        assert not profile.lazy_relationships, profile.lazy_relationships
        assert profile.sql_by_phase["collection"] == 0


@pytest.mark.parametrize("snapshot", [False, True], ids=["query", "snapshot"])
def test_diagnostics_reject_missing_or_wrong_namespace_roots(loaded_plate, snapshot):
    factory, graph = loaded_plate
    with pytest.raises(ValueError, match="not found"):
        profile_read(factory, graph.namespace_path, uuid4(), snapshot=snapshot)
    with pytest.raises(ValueError, match="namespace"):
        profile_read(factory, "", graph.plate_id, snapshot=snapshot)


def test_command_sql_is_attributed_to_mutation_flush_and_snapshot(loaded_plate):
    factory, graph = loaded_plate
    engine = factory.kw["bind"]
    with factory() as session:
        template_id = session.get(Resource, graph.plate_id).resource_template_id
    original_snapshot = CommandService._graph_result
    original_flush = Session.flush
    with (
        count_statements(engine) as count,
        capture_sql(engine, command_phases=True) as profile,
    ):
        result = CommandService(sessionmaker(bind=engine)).execute_batch(
            CreateResource(
                namespace_path=graph.namespace_path,
                name="Measured plate",
                template_id=template_id,
            ),
            build_local_command_context(),
        )
    assert count["n"] == profile.total_sql == sum(profile.sql_by_phase.values())
    assert all(
        profile.sql_by_phase[phase] > 0
        for phase in ("mutation", "flush", "snapshot_preparation")
    )
    assert profile.sql_by_phase["collection"] == 0
    assert profile.sql_by_kind["insert"] > 0
    assert profile.sql_by_kind["select"] > 0
    assert sum(profile.sql_by_kind.values()) == profile.total_sql
    assert profile.collected_records["snapshot_preparation"] == len(result.entities)
    assert CommandService._graph_result is original_snapshot
    assert Session.flush is original_flush
    assert not any(".pixi" in caller for caller in profile.callers)
    assert not profile.lazy_by_phase.get("mutation"), profile.lazy_by_phase
    assert not profile.lazy_by_phase.get("flush"), profile.lazy_by_phase


@pytest.mark.performance
@pytest.mark.parametrize("well_count", [12, 96], ids=["12-wells", "96-wells"])
def test_two_transfer_workflow_phase_budgets(client, well_count):
    inputs = setup_plate_workflow(client, well_count)
    profiles = {}

    @contextmanager
    def measure(label):
        with capture_sql(
            client.connection_state.engine, command_phases=True
        ) as profile:
            if label in {"source_reads", "children_read"}:
                profile.phase = "query"
            yield
        profiles[label] = profile

    result = record_loaded_plate(client, inputs, measure=measure)
    assert len(result.well_ids) == well_count
    for label, profile in profiles.items():
        assert profile.sql_by_phase["collection"] == 0, (label, profile.report())
        assert sum(profile.sql_by_kind.values()) == profile.total_sql
        assert sum(profile.sql_by_phase.values()) == profile.total_sql
        print(
            well_count,
            label,
            profile.total_sql,
            dict(profile.sql_by_phase),
            dict(profile.sql_by_kind),
            {phase: dict(counts) for phase, counts in profile.lazy_by_phase.items()},
            sep=" | ",
        )
        assert not profile.lazy_by_phase.get("flush"), (label, profile.lazy_by_phase)
        assert all(
            ".namespace]" in path for path in profile.lazy_by_phase.get("mutation", {})
        ), (label, profile.lazy_by_phase)
        if label == "create_run":
            assert profile.sql_by_kind["select"] <= 75
        if label == "finalize_run":
            assert profile.sql_by_kind["select"] <= 200
