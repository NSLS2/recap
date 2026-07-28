from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.db.namespace import Namespace
from recap.db.resource import Resource, ResourceTemplate
from recap.lifecycle import LifecycleStatus
from recap.schemas.resource import ResourceCopyChanges, ResourceCopyOptions


def _create_source(client):
    suffix = uuid4().hex
    uow = client.backend.begin()
    try:
        session = client.backend.session
        source_namespace = Namespace(
            path=f"copy-{suffix}", status=LifecycleStatus.ACTIVE
        )
        destination_namespace = Namespace(
            path=f"copy-{suffix}/destination",
            parent=source_namespace,
            status=LifecycleStatus.ACTIVE,
        )
        sibling_namespace = Namespace(
            path=f"copy-{suffix}-sibling", status=LifecycleStatus.ACTIVE
        )
        prefix_namespace = Namespace(
            path=f"copy-{suffix}x/destination", status=LifecycleStatus.ACTIVE
        )
        value_template = AttributeTemplate(
            name="value", value_type="array", default_value="[]"
        )
        group = AttributeGroupTemplate(
            name="details", attribute_templates=[value_template]
        )
        template = ResourceTemplate(
            name=f"resource-{suffix}",
            version="1",
            namespace=source_namespace,
            attribute_group_templates=[group],
        )
        root = Resource(name="root", template=template, namespace=source_namespace)
        child = Resource(
            name="child",
            template=template,
            namespace=source_namespace,
            parent=root,
        )
        grandchild = Resource(
            name="grandchild",
            template=template,
            namespace=source_namespace,
            parent=child,
        )
        root.properties["details"]._values["value"].value = [1, {"nested": [2]}]
        root.properties["details"]._values["value"].metadata_json = {"tags": ["source"]}
        child.properties["details"]._values["value"].value = [3]
        grandchild.properties["details"]._values["value"].value = [4]
        session.add_all(
            [
                source_namespace,
                destination_namespace,
                sibling_namespace,
                prefix_namespace,
                template,
                root,
            ]
        )
        session.flush()
        result = {
            "source_id": root.id,
            "child_id": child.id,
            "source_namespace_id": source_namespace.id,
            "destination_namespace_id": destination_namespace.id,
            "sibling_namespace_id": sibling_namespace.id,
            "prefix_namespace_id": prefix_namespace.id,
        }
        uow.commit()
        return result
    except Exception:
        uow.rollback()
        raise


def _load_tree(client, root_id):
    uow = client.backend.begin()
    try:
        resources = client.backend._load_resource_subtrees(
            client.backend.session, [root_id]
        )
        by_id = {resource.id: resource for resource in resources}
        root = by_id[root_id]
        # Snapshot while ORM relationships remain attached to active session.
        snapshot = {
            "root": root,
            "resources": list(resources),
            "values": {
                resource.name: deepcopy(
                    resource.properties["details"]._values["value"].value
                )
                for resource in resources
            },
            "metadata": deepcopy(
                root.properties["details"]._values["value"].metadata_json
            ),
        }
        uow.commit()
        return snapshot
    except Exception:
        uow.rollback()
        raise


def test_copy_resource_deep_copies_full_graph_with_new_ids(client):
    setup = _create_source(client)

    copied = client.copy_resource(setup["source_id"], setup["destination_namespace_id"])

    source = _load_tree(client, setup["source_id"])
    clone = _load_tree(client, copied.id)
    source_by_name = {resource.name: resource for resource in source["resources"]}
    clone_by_name = {resource.name: resource for resource in clone["resources"]}
    assert set(clone_by_name) == {"root", "child", "grandchild"}
    for name in clone_by_name:
        assert clone_by_name[name].id != source_by_name[name].id
        assert (
            clone_by_name[name].properties["details"].id
            != source_by_name[name].properties["details"].id
        )
        clone_value = clone_by_name[name].properties["details"]._values["value"]
        source_value = source_by_name[name].properties["details"]._values["value"]
        assert clone_value.id != source_value.id
        assert clone_value.value == source_value.value
    assert clone["root"].copied_from_id == setup["source_id"]
    assert all(
        resource.copied_from_id is None
        for resource in clone["resources"]
        if resource.id != copied.id
    )


def test_copy_resource_isolates_mutable_values_and_applies_root_overrides(client):
    setup = _create_source(client)

    copied = client.copy_resource(
        setup["source_id"],
        setup["destination_namespace_id"],
        ResourceCopyOptions(
            name="copy",
            changes=ResourceCopyChanges(properties={"details": {"value": [9]}}),
        ),
    )

    uow = client.backend.begin()
    clone = client.backend.session.get(Resource, copied.id)
    copied_value = clone.properties["details"]._values["value"]
    copied_value.value = [*copied_value.value, 10]
    copied_value.metadata_json["tags"] = [*copied_value.metadata_json["tags"], "copy"]
    uow.commit()

    source = _load_tree(client, setup["source_id"])
    clone = _load_tree(client, copied.id)
    assert clone["root"].name == "copy"
    assert clone["values"]["copy"] == [9, 10]
    assert source["values"]["root"] == [1, {"nested": [2]}]
    assert clone["metadata"] == {"tags": ["source", "copy"]}
    assert source["metadata"] == {"tags": ["source"]}


def test_copy_resource_activates_source_and_owns_clone_in_destination(client):
    setup = _create_source(client)

    copied = client.copy_resource(setup["source_id"], setup["destination_namespace_id"])

    source = _load_tree(client, setup["source_id"])["root"]
    clone = _load_tree(client, copied.id)
    assert source.status is LifecycleStatus.ACTIVE
    assert all(
        resource.namespace_id == setup["destination_namespace_id"]
        for resource in clone["resources"]
    )
    assert all(
        resource.status is LifecycleStatus.MUTABLE and resource.revision == 1
        for resource in clone["resources"]
    )


@pytest.mark.parametrize(
    "destination_key", ["sibling_namespace_id", "prefix_namespace_id"]
)
def test_copy_resource_rejects_destination_outside_source_ancestry(
    client, destination_key
):
    setup = _create_source(client)

    with pytest.raises(ValueError, match="descendant"):
        client.copy_resource(setup["source_id"], setup[destination_key])


def test_copy_resource_requires_source_graph_root(client):
    setup = _create_source(client)

    with pytest.raises(ValueError, match="root"):
        client.copy_resource(setup["child_id"], setup["destination_namespace_id"])


def test_invalid_copy_changes_roll_back_source_activation_and_clone(client):
    setup = _create_source(client)

    with pytest.raises(ValueError, match="missing"):
        client.copy_resource(
            setup["source_id"],
            setup["destination_namespace_id"],
            ResourceCopyOptions(
                changes=ResourceCopyChanges(properties={"missing": {"value": 9}})
            ),
        )

    uow = client.backend.begin()
    try:
        session = client.backend.session
        source = session.get(Resource, setup["source_id"])
        clone_count = session.scalar(
            select(func.count())
            .select_from(Resource)
            .where(Resource.namespace_id == setup["destination_namespace_id"])
        )
        assert source.status is LifecycleStatus.MUTABLE
        assert clone_count == 0
        uow.commit()
    except Exception:
        uow.rollback()
        raise
