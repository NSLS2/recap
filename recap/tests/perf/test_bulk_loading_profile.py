"""Cold graph preparation and workflow SQL budgets."""

from contextlib import contextmanager

import pytest

from recap.db.resource import Resource
from recap.tests.helpers.entity_fixtures import (
    record_loaded_plate,
    setup_plate_workflow,
)
from recap.tests.helpers.loading import read_graph
from recap.tests.helpers.sql_profile import capture_sql, profile_read


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


@pytest.mark.parametrize("root", ["plate", "well", "run"])
@pytest.mark.performance
def test_snapshot_collection_issues_no_sql(loaded_plate, root):
    factory, graph = loaded_plate
    kind = "process_run" if root == "run" else "resource"
    _, profile = read_graph(factory, graph, root, kind, (), True)
    assert profile.sql_by_phase["collection"] == 0
    assert profile.total_sql <= (50 if root == "run" else 20), profile.report()


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
