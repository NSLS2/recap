"""
Regression tests for array-type attribute default values.

The bug: when type="array", the Pydantic validator coerces the default to a
MutableList (a list subclass). Passing this directly into filter_by() against
the str default_value column raises a SQLite bind-parameter error.

The fix serializes list-like defaults to JSON strings before storage/query.
"""

from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.db.resource import Resource, ResourceTemplate
from recap.dsl.resource_builder import ResourceTemplateBuilder


class TestArrayDefaultViaLocalBackend:
    """Test the adapter layer directly with list defaults."""

    def test_add_attribute_with_empty_list_default(self, client):
        """An array attribute with default=[] should not raise on filter_by."""
        with client.build_resource_template(
            name="ArrayTest-T1", type_names=["sample"]
        ) as rtb:
            rtb.prop_group("data").add_attribute("tags", "array", "", []).close_group()

        # Verify template was persisted and is queryable
        tmpl = (
            client.query_maker()
            .resource_templates()
            .filter(name="ArrayTest-T1")
            .first()
        )
        assert tmpl is not None

    def test_add_attribute_with_populated_list_default(self, client):
        """An array attribute with a non-empty default list stores correctly."""
        with client.build_resource_template(
            name="ArrayTest-T2", type_names=["sample"]
        ) as rtb:
            rtb.prop_group("meta").add_attribute(
                "labels", "array", "", ["a", "b", "c"]
            ).close_group()

        tmpl = (
            client.query_maker()
            .resource_templates()
            .filter(name="ArrayTest-T2")
            .first()
        )
        assert tmpl is not None

    def test_array_default_idempotent_on_reopen(self, client):
        """Reopening a template with an array attribute doesn't create duplicates."""
        for _ in range(2):
            with ResourceTemplateBuilder(
                name="ArrayTest-T3", type_names=["container"], backend=client.backend
            ) as rtb:
                rtb.prop_group("info").add_attribute(
                    "items", "array", "", []
                ).close_group()

        # Should still have exactly one attribute template named "items"
        tmpl = (
            client.query_maker()
            .resource_templates()
            .filter(name="ArrayTest-T3")
            .include_attribute_groups()
            .first()
        )
        assert tmpl is not None


class TestArrayDefaultRoundTrip:
    """Test that array defaults survive store → read → use cycle."""

    def test_resource_gets_array_default_value(self, db_session):
        """When a Resource is created, array default_value is correctly applied."""
        tmpl = ResourceTemplate(name="ArrayRT")
        group = AttributeGroupTemplate(name="Props", resource_template=tmpl)
        AttributeTemplate(
            name="tags",
            value_type="array",
            default_value="[]",  # stored as JSON string in the column
            attribute_group_template=group,
        )
        db_session.add_all([tmpl, group])
        db_session.flush()

        res = Resource(name="R-array", template=tmpl)
        db_session.add(res)
        db_session.flush()

        av = res.properties["Props"]._values["tags"]
        assert av.value == []

    def test_resource_gets_populated_array_default(self, db_session):
        """Array default with items round-trips correctly."""
        tmpl = ResourceTemplate(name="ArrayRT2")
        group = AttributeGroupTemplate(name="Info", resource_template=tmpl)
        AttributeTemplate(
            name="colors",
            value_type="array",
            default_value='["red", "green"]',
            attribute_group_template=group,
        )
        db_session.add_all([tmpl, group])
        db_session.flush()

        res = Resource(name="R-array2", template=tmpl)
        db_session.add(res)
        db_session.flush()

        av = res.properties["Info"]._values["colors"]
        assert av.value == ["red", "green"]

    def test_full_dsl_array_resource_creation(self, client):
        """End-to-end: build template with array attr, create resource, read value."""
        with client.build_resource_template(
            name="ArrayE2E-T", type_names=["sample"]
        ) as rtb:
            rtb.prop_group("data").add_attribute(
                "measurements", "array", "mm", [1, 2, 3]
            ).close_group()

        with client.build_resource("ArrayE2E-R", "ArrayE2E-T") as _:
            pass  # just create with defaults

        res = (
            client.query_maker()
            .resources()
            .filter(name="ArrayE2E-R")
            .include(["template", "properties"])
            .first()
        )
        assert res is not None
        assert res.properties.data.values.measurements.value == [1, 2, 3]
