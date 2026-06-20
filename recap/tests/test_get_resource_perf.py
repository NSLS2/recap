"""Performance + correctness tests for ``get_resource(expand=True)``.

``get_resource(expand=True)`` must hydrate the whole resource subtree with a
**bounded, depth-independent** number of SQL statements: it bulk-fetches the
root and all descendants in one query and builds the schema tree from that
flat result.

The regression these tests guard against is hydrating the tree by walking
``resource.children`` instead, which lazy-loads each level on demand — an N+1
that grows with tree depth.
"""

from recap.dsl.resource_builder import ResourceTemplateBuilder

from .conftest import count_statements


def _make_template(client, name):
    """A minimal single-type template with one property group."""
    with ResourceTemplateBuilder(
        name=name, type_names=["container"], backend=client.backend
    ) as rtb:
        rtb.prop_group("details").add_attribute(
            "serial", "str", "", "abc"
        ).close_group()


def _make_chain(client, depth, *, template, prefix):
    """Create a linear resource chain root -> c1 -> ... of ``depth`` nodes.

    Returns the root :class:`ResourceSchema`.
    """
    root = client.create_resource(f"{prefix}-0", template, on_existing="create")
    parent = root
    for level in range(1, depth):
        parent = client.create_resource(
            f"{prefix}-{level}",
            template,
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


def test_get_resource_expand_is_depth_independent(client):
    """``get_resource(expand=True)`` must issue a bounded, depth-independent
    number of SQL statements regardless of tree depth."""
    _make_template(client, "GetResPerfT")
    _make_chain(client, depth=3, template="GetResPerfT", prefix="three")
    _make_chain(client, depth=4, template="GetResPerfT", prefix="four")

    with count_statements(client) as counter_3:
        tree_3 = client.get_resource("three-0", "GetResPerfT", expand=True)
    n_three = counter_3["n"]

    with count_statements(client) as counter_4:
        tree_4 = client.get_resource("four-0", "GetResPerfT", expand=True)
    n_four = counter_4["n"]

    # Correctness: the full depth must be hydrated.
    assert _walk_depth(tree_3) == 3
    assert _walk_depth(tree_4) == 4

    # Regression guard: statement count must not grow with depth.
    assert n_four == n_three, (
        f"get_resource(expand=True) is depth-dependent (N+1): "
        f"3-level={n_three} statements, 4-level={n_four} statements"
    )


def test_get_resource_expand_matches_query_full(client):
    """``get_resource(expand=True)`` must return the same hydrated tree as a
    ``load="full"`` query for the same root (behaviour-preserving refactor)."""
    _make_template(client, "GetResEqT")
    _make_chain(client, depth=4, template="GetResEqT", prefix="eq")

    got = client.get_resource("eq-0", "GetResEqT", expand=True)

    qm = client.query_maker(unscoped=True)
    expected = qm.resources(load="full").filter(name="eq-0").first()

    assert got.id == expected.id
    assert got.name == expected.name

    # Walk both trees in lockstep; compare structure + a sample property value.
    def _flatten(resource):
        out = {resource.name: resource}
        for child in resource.children.values():
            out.update(_flatten(child))
        return out

    got_nodes = _flatten(got)
    expected_nodes = _flatten(expected)
    assert set(got_nodes) == set(expected_nodes)

    for name, got_node in got_nodes.items():
        exp_node = expected_nodes[name]
        assert got_node.id == exp_node.id
        assert set(got_node.children) == set(exp_node.children)
        got_node.build_property_model()
        exp_node.build_property_model()
        assert (
            got_node.properties.details.serial.value
            == exp_node.properties.details.serial.value
        )
