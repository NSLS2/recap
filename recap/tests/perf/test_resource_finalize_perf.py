"""Bulk finalization has bounded write costs, not one transaction per child."""

from contextlib import contextmanager
from time import perf_counter

import pytest
from sqlalchemy import event

from recap.tests.conftest import count_statements


@contextmanager
def _count_writes(engine):
    counts = {"transactions": 0, "resource_updates": 0}

    def commit(connection):
        counts["transactions"] += 1

    def statement(connection, cursor, sql, parameters, context, executemany):
        if (
            context.compiled is not None
            and context.compiled.isupdate
            and context.compiled.statement.table.name == "resource"
        ):
            counts["resource_updates"] += 1

    event.listen(engine, "commit", commit)
    event.listen(engine, "before_cursor_execute", statement)
    try:
        yield counts
    finally:
        event.remove(engine, "commit", commit)
        event.remove(engine, "before_cursor_execute", statement)


def _make_tree(client, *, prefix, width=1, depth=1):
    with client.build_resource(f"{prefix}-root", "Finalize node") as root:
        for index in range(width):
            parent = root
            for level in range(depth):
                parent = parent.add_child(f"{prefix}-{index}-{level}", "Finalize node")
    return root


def _setup_template(client):
    with client.build_resource_template(
        name="Finalize node", type_names=["container"]
    ) as template:
        template.add_properties(
            {"details": [{"name": "note", "type": "str", "default": "value"}]}
        )


@pytest.mark.performance
@pytest.mark.parametrize("width", [12, 96])
def test_recursive_finalization_one_transaction_with_pending_children(client, width):
    _setup_template(client)
    builder = _make_tree(client, prefix=f"plate-{width}", width=width)
    for child in builder._children:
        child.finalize()
    with _count_writes(client.connection_state.engine) as counts, builder:
        builder.finalize(recursive_depth=None)
    assert counts["transactions"] == 1
    assert counts["resource_updates"] == 2  # revision guard + set-based activation
    assert len(builder.resource.children) == width
    assert all(
        child.status.value == "ACTIVE" and child.revision == 2
        for child in builder.resource.children.values()
    )


@pytest.mark.performance
def test_recursive_finalization_statement_count_is_depth_independent(client):
    _setup_template(client)
    counts = []
    for depth in [3, 8]:
        tree = _make_tree(client, prefix=f"chain-{depth}", depth=depth)
        # Initialization is deliberately outside the measurement.
        builder = client.build_resource(resource_id=tree.resource.id)
        with (
            count_statements(client) as counter,
            _count_writes(client.connection_state.engine) as writes,
            builder,
        ):
            builder.finalize(recursive_depth=None)
        counts.append(counter["n"])
        assert writes == {"transactions": 1, "resource_updates": 2}
    assert counts[0] == counts[1]
    assert counts[1] <= 25


@pytest.mark.performance
def test_recursive_finalization_statement_count_is_width_independent(
    client, record_property
):
    _setup_template(client)
    counts = []
    for width in [12, 96]:
        tree = _make_tree(client, prefix=f"wide-{width}", width=width)
        builder = client.build_resource(resource_id=tree.resource.id)
        start = perf_counter()
        with count_statements(client) as counter, builder:
            builder.finalize(recursive_depth=None)
        counts.append(counter["n"])
        record_property(f"finalize_{width}_seconds", perf_counter() - start)
    assert counts[0] == counts[1]
    assert counts[1] <= 25
