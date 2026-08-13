"""Performance + correctness tests for ``include(["properties"])`` hydration.

A resource query with ``include(["properties"])`` must eager-load everything
``build_property_model()`` touches -- including each property's
``template`` (the ``AttributeGroupTemplate``) and that template's
``attribute_templates``. Otherwise calling ``build_property_model()`` on a
returned schema lazy-loads the template per property group: an N+1 that scales
with the number of property groups.

The regression guard is that calling ``build_property_model()`` on resources
fetched with ``include(["properties"])`` issues **zero** additional SQL
statements.
"""

import pytest

from .conftest import count_statements


def _make_template(client, name="IncPropT"):
    """A template with three property groups, each with one attribute."""
    with client.build_resource_template(name=name, type_names=["container"]) as rtb:
        rtb.prop_group("details").add_attribute(
            "serial", "str", "", "abc"
        ).close_group()
        rtb.prop_group("dimensions").add_attribute(
            "height", "int", "", "0"
        ).close_group()
        rtb.prop_group("status").add_attribute("state", "str", "", "new").close_group()


@pytest.mark.performance
def test_build_property_model_after_include_properties_no_lazy_loads(client):
    """build_property_model() on a resource fetched with include(["properties"])
    must not trigger any additional SQL (no per-group template lazy load)."""
    _make_template(client)
    first = client.create_resource("incprop-a", "IncPropT", on_existing="create")
    second = client.create_resource("incprop-b", "IncPropT", on_existing="create")
    for resource in (first, second):
        client.build_resource(resource_id=resource.id).activate()

    qm = client.query_maker()
    resources = qm.resources().include(["properties"]).filter(name="incprop-a").all()

    # All hydration SQL should have run during .all(); building the dynamic
    # property model accesses each property's template, which must already be
    # loaded -- it must not trigger any further SQL.
    with count_statements(client) as counter:
        for resource in resources:
            resource.build_property_model()
            # Touch the materialised values to confirm they are loaded too.
            assert resource.properties.details.values.serial.value == "abc"
            assert resource.properties.dimensions.values.height.value == 0
            assert resource.properties.status.values.state.value == "new"

    assert counter["n"] == 0, (
        f"build_property_model() after include(['properties']) issued "
        f"{counter['n']} lazy SQL statements; expected 0 (Property.template "
        f"should be eager-loaded by the properties preload)"
    )


@pytest.mark.performance
def test_include_properties_matches_load_eager_statement_count(client):
    """include(['properties']) must load property templates as efficiently as
    the load="eager" path. Both go through
    the same eager-load chain for Property.template / _values, so a query +
    build_property_model() must issue the same number of statements either way.

    This guards against include(['properties']) regressing to lazy-load
    Property.template per group while load="eager" does not.
    """
    _make_template(client, name="IncPropParity")
    resource = client.create_resource(
        "parity-res", "IncPropParity", on_existing="create"
    )
    client.build_resource(resource_id=resource.id).activate()

    qm = client.query_maker()

    with count_statements(client) as c_include:
        inc = qm.resources().include(["properties"]).filter(name="parity-res").all()
        for r in inc:
            r.build_property_model()

    with count_statements(client) as c_eager:
        eager = qm.resources(load="eager").filter(name="parity-res").all()
        for r in eager:
            r.build_property_model()

    assert c_include["n"] <= c_eager["n"], (
        f"include(['properties']) issues more statements than load='eager': "
        f"include={c_include['n']}, eager={c_eager['n']}"
    )
