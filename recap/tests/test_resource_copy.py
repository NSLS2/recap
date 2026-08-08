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
    source_path = f"copy-{suffix}"
    destination_path = f"{source_path}/destination"
    sibling_path = f"{source_path}-sibling"
    prefix_path = f"{source_path}x/destination"
    namespace_prefix = client.namespace_path
    source_full_path = "/".join(filter(None, (namespace_prefix, source_path)))
    destination_full_path = "/".join(
        filter(None, (namespace_prefix, destination_path))
    )
    sibling_full_path = "/".join(filter(None, (namespace_prefix, sibling_path)))
    prefix_full_path = "/".join(filter(None, (namespace_prefix, prefix_path)))
    uow = client.backend.begin()
    try:
        session = client.backend.session
        source_namespace = Namespace(
            path=source_full_path, status=LifecycleStatus.ACTIVE
        )
        destination_namespace = Namespace(
            path=destination_full_path,
            parent=source_namespace,
            status=LifecycleStatus.ACTIVE,
        )
        sibling_namespace = Namespace(
            path=sibling_full_path, status=LifecycleStatus.ACTIVE
        )
        prefix_namespace = Namespace(
            path=prefix_full_path, status=LifecycleStatus.ACTIVE
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
        root.properties["details"]._values["value"].unit = "uL"
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
            "destination_path": destination_path,
            "sibling_path": sibling_path,
            "prefix_path": prefix_path,
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

    copied = client.namespace(setup["destination_path"]).copy_resource(
        setup["source_id"]
    )

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

    copied = client.namespace(setup["destination_path"]).copy_resource(
        setup["source_id"],
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


def test_builder_copy_on_write_preserves_value_metadata(client):
    setup = _create_source(client)
    uow = client.backend.begin()
    client.backend.session.get(Resource, setup["source_id"]).activate()
    uow.commit()
    client.set_namespace(setup["source_namespace_id"])

    with client.build_resource(resource_id=setup["source_id"]) as builder:
        value = builder.resource.properties["details"].values["value"]
        value.unit = "mL"
        value.metadata_json = {"tags": ["builder"]}
    copied_id = builder.resource.id

    source = _load_tree(client, setup["source_id"])["root"]
    copied = _load_tree(client, copied_id)["root"]
    copied_value = copied.properties["details"]._values["value"]
    assert copied_value.unit == "mL"
    assert copied_value.metadata_json == {"tags": ["builder"]}
    assert source.status is LifecycleStatus.ACTIVE


def test_copy_resource_activates_source_and_owns_clone_in_destination(client):
    setup = _create_source(client)

    copied = client.namespace(setup["destination_path"]).copy_resource(
        setup["source_id"]
    )

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
    "destination_key", ["sibling_path", "prefix_path"]
)
def test_copy_resource_rejects_destination_outside_source_ancestry(
    client, destination_key
):
    setup = _create_source(client)

    with pytest.raises(ValueError, match="descendant"):
        client.namespace(setup[destination_key]).copy_resource(setup["source_id"])


def test_copy_resource_requires_source_graph_root(client):
    setup = _create_source(client)

    with pytest.raises(ValueError, match="root"):
        client.namespace(setup["destination_path"]).copy_resource(setup["child_id"])


def test_invalid_copy_changes_roll_back_source_activation_and_clone(client):
    setup = _create_source(client)

    with pytest.raises(ValueError, match="missing"):
        client.namespace(setup["destination_path"]).copy_resource(
            setup["source_id"],
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
