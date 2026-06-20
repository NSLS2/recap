"""Performance + correctness tests for resource-tree hydration.

A ``load="full"`` / ``include("children")`` resource query must hydrate the
whole tree with a **bounded, depth-independent** number of SQL statements: the
root and all descendants are bulk-fetched in one query and the schema tree is
built from that flat result.

The regression these tests guard against is hydrating the tree by walking
``resource.children.values()`` in Python, which lazy-loads each node on demand —
an N+1 that scales with tree size.

The key assertion is *depth-independence*: a 3-level and a 4-level chain must
issue the **same** number of statements.
"""

from recap.dsl.resource_builder import ResourceTemplateBuilder

from .conftest import count_statements


def _make_template(client, name="TreePerfT"):
    """A minimal single-type template with one property group."""
    with ResourceTemplateBuilder(
        name=name, type_names=["container"], backend=client.backend
    ) as rtb:
        rtb.prop_group("details").add_attribute(
            "serial", "str", "", "abc"
        ).close_group()


def _make_chain(client, depth, *, prefix):
    """Create a linear resource chain root -> c1 -> ... of ``depth`` nodes.

    Returns the root :class:`ResourceSchema`.
    """
    root = client.create_resource(f"{prefix}-0", "TreePerfT", on_existing="create")
    parent = root
    for level in range(1, depth):
        parent = client.create_resource(
            f"{prefix}-{level}",
            "TreePerfT",
            parent=parent,
            on_existing="create",
        )
    return root


def _walk_depth(resource):
    """Return the number of nodes along the (single) child chain."""
    n = 1
    children = list(resource.children.values())
    while children:
        n += 1
        children = list(children[0].children.values())
    return n


def test_load_full_resource_tree_is_depth_independent(client):
    """A ``load="full"`` resource query must issue a bounded, depth-independent
    number of SQL statements regardless of tree depth."""
    _make_template(client)
    _make_chain(client, depth=3, prefix="three")
    _make_chain(client, depth=4, prefix="four")

    qm = client.query_maker(unscoped=True)

    with count_statements(client) as counter_3:
        tree_3 = qm.resources(load="full").filter(name="three-0").first()
    n_three = counter_3["n"]

    with count_statements(client) as counter_4:
        tree_4 = qm.resources(load="full").filter(name="four-0").first()
    n_four = counter_4["n"]

    # Correctness: the full depth must be hydrated.
    assert _walk_depth(tree_3) == 3
    assert _walk_depth(tree_4) == 4

    # Regression guard: statement count must not grow with depth.
    assert n_four == n_three, (
        f"resource-tree hydration is depth-dependent (N+1): "
        f"3-level={n_three} statements, 4-level={n_four} statements"
    )


def test_load_full_resource_tree_bounded_count(client):
    """The absolute statement count for a deep tree must be a small constant."""
    _make_template(client, name="TreePerfBounded")
    root = client.create_resource("bounded-0", "TreePerfBounded", on_existing="create")
    parent = root
    for level in range(1, 5):
        parent = client.create_resource(
            f"bounded-{level}",
            "TreePerfBounded",
            parent=parent,
            on_existing="create",
        )

    qm = client.query_maker(unscoped=True)
    with count_statements(client) as counter:
        tree = qm.resources(load="full").filter(name="bounded-0").first()

    assert _walk_depth(tree) == 5
    # Bulk CTE + a fixed set of selectinload batches (one per distinct
    # template/attribute-group, not per resource). The pre-fix path issued one
    # lazy load per node; this asserts a depth-independent constant instead.
    assert counter["n"] <= 18, f"expected bounded count, got {counter['n']}"
