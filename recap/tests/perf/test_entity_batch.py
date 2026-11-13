"""Flat batch SQL budgets."""

import pytest
from sqlalchemy.orm import sessionmaker

from recap.adapter.local import LocalBackend
from recap.dsl.query import QuerySpec
from recap.schemas.process import ProcessRunSchema
from recap.schemas.resource import ResourceSchema
from recap.tests.conftest import count_statements
from recap.tests.helpers.entity_fixtures import _seed_resource_graph
from recap.tests.perf.test_process_run_resource_tree_perf import (
    _seed_run_with_resource_chain,
)


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
            from recap.tests.perf.test_process_run_resource_tree_perf import _walk_depth

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
