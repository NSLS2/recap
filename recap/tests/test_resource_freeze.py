import pytest

from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.db.namespace import Namespace
from recap.db.resource import Resource, ResourceTemplate
from recap.lifecycle import LifecycleStatus


def _resource_tree(db_session):
    namespace = Namespace(path="/resource-freeze", status=LifecycleStatus.ACTIVE)
    value_template = AttributeTemplate(
        name="value", value_type="int", default_value="1"
    )
    group = AttributeGroupTemplate(
        name="properties", attribute_templates=[value_template]
    )
    template = ResourceTemplate(
        name="resource",
        version="1",
        namespace=namespace,
        attribute_group_templates=[group],
    )
    root = Resource(name="root", template=template, namespace=namespace)
    child = Resource(name="child", template=template, namespace=namespace, parent=root)
    db_session.add(root)
    db_session.flush()
    return root, child


def test_mutable_resource_allows_nested_value_update(db_session):
    root, child = _resource_tree(db_session)
    child.properties["properties"].values["value"] = 2
    db_session.flush()
    assert child.properties["properties"].values["value"] == 2
    assert root.status is LifecycleStatus.MUTABLE


@pytest.mark.parametrize("target", ["root", "child", "value"])
def test_active_resource_rejects_aggregate_mutation(db_session, target):
    root, child = _resource_tree(db_session)
    root.activate()
    db_session.flush()

    if target == "root":
        root.name = "changed-root"
    elif target == "child":
        child.name = "changed-child"
    else:
        child.properties["properties"].values["value"] = 2

    with pytest.raises(ValueError, match="active resource"):
        db_session.flush()


def test_active_resource_rejects_child_deletion(db_session):
    root, child = _resource_tree(db_session)
    root.activate()
    db_session.flush()
    db_session.delete(child)

    with pytest.raises(ValueError, match="active resource"):
        db_session.flush()
