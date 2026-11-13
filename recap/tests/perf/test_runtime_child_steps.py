"""Runtime child loading performance."""

import pytest

from recap.tests.helpers.entity_fixtures import _run


@pytest.mark.performance
def test_runtime_child_loading_has_bounded_statement_count(client):
    from recap.tests.conftest import count_statements

    scoped, run = _run(client, "child-loading")
    with scoped.build_process_run(process_run_id=run.id) as builder:
        builder.add_child_step(
            builder.process_run.steps["Transfer"].generate_child(name="first")
        )
    engine = client.connection_state.backend.reader._session_factory.kw["bind"]
    with count_statements(engine) as small:
        scoped.build_process_run(process_run_id=run.id)
    with scoped.build_process_run(process_run_id=run.id) as builder:
        for index in range(96):
            builder.add_child_step(
                builder.process_run.steps["Transfer"].generate_child(
                    name=f"well-{index}"
                )
            )
    with count_statements(engine) as large:
        loaded = scoped.build_process_run(process_run_id=run.id)
    assert len(loaded.process_run.steps["Transfer"].children) == 97
    assert large["n"] <= small["n"] + 1
