from uuid import uuid4

import pytest


def _resource_client(client):
    client.create_namespace("builder-identity")
    return client


def _make_resource(client):
    scoped = _resource_client(client)
    with scoped.build_resource_template(
        name="IdentityRT", type_names=["container"]
    ) as template:
        template.prop_group("details").add_attribute(
            "serial", "str", "", "old"
        ).close_group()
    return scoped, scoped.create_resource("identity-resource", "IdentityRT")


def test_resource_exception_discards_detached_draft(client):
    scoped, resource = _make_resource(client)

    with (
        pytest.raises(RuntimeError, match="abort"),
        scoped.build_resource(resource_id=resource.id) as builder,
    ):
        draft = builder.get_model()
        draft.name = "unsaved"
        builder.set_model(draft)
        raise RuntimeError("abort")

    assert resource.name == "identity-resource"


def test_resource_save_merges_into_held_canonical_reference(client):
    scoped, resource = _make_resource(client)

    with scoped.build_resource(resource_id=resource.id) as builder:
        draft = builder.get_model()
        draft.properties.details.values.serial.value = "saved"
        builder.set_model(draft)

    assert resource.properties.details.values.serial.value == "saved"


def test_resource_set_model_rejects_mismatched_uuid(client):
    scoped, resource = _make_resource(client)

    with scoped.build_resource(resource_id=resource.id) as builder:
        draft = builder.get_model()
        draft = draft.model_copy(update={"id": uuid4()})
        with pytest.raises(ValueError, match="ID"):
            builder.set_model(draft)


def test_new_resource_get_model_is_copy_on_write(client):
    scoped = _resource_client(client)
    with scoped.build_resource_template(name="CopyRT", type_names=["container"]):
        pass

    builder = scoped.build_resource("copy-resource", "CopyRT")
    draft = builder.get_model()
    builder.save()
    assert draft.id != builder.resource.id
    draft.name = "changed"
    assert builder.resource.name == "copy-resource"
