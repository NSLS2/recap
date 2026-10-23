"""Cold distinct-template graphs expose preparation and extraction SQL separately."""

import hashlib
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from recap.adapter.entity_batch import EntityBatchCollector
from recap.client.identity import IdentityMap
from recap.client.materialization import BatchMaterializer
from recap.db.resource import Resource
from recap.schemas.process import ProcessRunSchema
from recap.schemas.resource import ResourceSchema
from scripts.profile_bulk_loading import (
    capture_sql,
    profile_cases,
    profile_read,
    readonly_engine,
)

from .conftest import count_statements
from .entity_fixtures import seed_loaded_well_plate


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
    assert profile.lazy_relationships
    assert profile.planned_relationships
    assert profile.callers
    assert batch.roots[0].id == graph.plate_id


def test_capture_ignores_other_engines_and_cleans_up_on_error(loaded_plate):
    factory, graph = loaded_plate
    other = create_engine(factory.kw["bind"].url)
    try:
        with pytest.raises(RuntimeError), capture_sql(factory.kw["bind"]) as profile:
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
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="Mapped collection keys lazily load property/parameter/value templates",
)
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
    ), profile.lazy_relationships


@pytest.mark.performance
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="Children-only subtree preparation loads unused properties and values",
)
def test_children_only_does_not_load_unused_values(loaded_plate):
    factory, graph = loaded_plate
    _, profile = read_graph(factory, graph, "plate", "resource", ("children",), False)
    assert profile.loaded_types["Property"] == 0
    assert profile.loaded_types["AttributeValue"] == 0


@pytest.mark.performance
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="Well template ancestor/sibling closure is not bulk prepared",
)
def test_well_eager_collection_issues_no_sql(loaded_plate):
    factory, graph = loaded_plate
    _, profile = read_graph(factory, graph, "well", "resource", (), False)
    assert profile.sql_by_phase["collection"] == 0


@pytest.mark.parametrize("root", ["plate", "well", "run"])
@pytest.mark.performance
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="Command snapshots collect unprepared ORM graphs",
)
def test_snapshot_collection_issues_no_sql(loaded_plate, root):
    factory, graph = loaded_plate
    kind = "process_run" if root == "run" else "resource"
    _, profile = read_graph(factory, graph, root, kind, (), True)
    assert profile.sql_by_phase["collection"] == 0


@pytest.mark.parametrize("snapshot", [False, True], ids=["query", "snapshot"])
def test_diagnostics_reject_missing_or_wrong_namespace_roots(loaded_plate, snapshot):
    factory, graph = loaded_plate
    with pytest.raises(ValueError, match="not found"):
        profile_read(factory, graph.namespace_path, uuid4(), snapshot=snapshot)
    with pytest.raises(ValueError, match="namespace"):
        profile_read(factory, "", graph.plate_id, snapshot=snapshot)
