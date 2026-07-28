import pytest

from recap.db.attribute import AttributeTemplate
from recap.db.process import ProcessTemplate
from recap.db.resource import Resource, ResourceTemplate
from recap.lifecycle import LifecycleStatus
from recap.utils.general import Direction


def test_resource_template_guard_prevents_updates_when_resources_exist(client):
    client.create_campaign("Resource Guard", "P0", None)
    with client.build_resource_template(
        name="GuardedTemplate", type_names=["sample"], version="1.0"
    ):
        pass

    with client.build_resource("Res1", "GuardedTemplate") as rb:
        resource = rb.resource

    uow = client.backend.begin()
    try:
        tmpl_model = (
            client.backend.session.query(ResourceTemplate)
            .filter_by(name="GuardedTemplate", version="1.0")
            .one()
        )
        tmpl_model.name = "ShouldFail"
        with pytest.raises(ValueError, match="active resource template"):
            client.backend.session.flush()
    finally:
        uow.rollback()

    # Ensure the original resource is still intact
    uow = client.backend.begin()
    fetched = client.backend.session.query(Resource).filter_by(name=resource.name).one()
    assert fetched is not None
    assert fetched.template.name == "GuardedTemplate"
    uow.rollback()


def test_process_template_guard_prevents_updates_when_runs_exist(client):
    client.create_campaign("Camp", "P1", "SAF1")
    with client.build_resource_template(
        name="ProcRes", type_names=["container"], version="1.0"
    ):
        pass
    resource = client.create_resource("ProcRes1", "ProcRes")

    with client.build_process_template(name="PT Guard", version="1.0") as pt_builder:
        pt_builder.add_resource_slot(
            "slot1",
            "container",
            direction=Direction.input,
            create_resource_type=True,
        ).add_step("step1").param_group("pg").add_attribute(
            "x", "int", "", 0
        ).close_group().close_step()

    with client.build_process_run("RunGuard", "desc", "PT Guard", "1.0") as prb:
        prb.assign_resource("slot1", resource)

    uow = client.backend.begin()
    try:
        pt_model = (
            client.backend.session.query(ProcessTemplate)
            .filter_by(name="PT Guard", version="1.0")
            .one()
        )
        pt_model.name = "ShouldFail"
        with pytest.raises(ValueError, match="active process template"):
            client.backend.session.flush()
    finally:
        uow.rollback()

    uow = client.backend.begin()
    unchanged = (
        client.backend.session.query(ProcessTemplate)
        .filter_by(name="PT Guard", version="1.0")
        .one()
    )
    assert unchanged.name == "PT Guard"
    uow.rollback()


def test_active_process_template_rejects_nested_attribute_mutation(client):
    client.create_campaign("Nested Guard", "P2", None)
    with client.build_process_template(name="Nested PT", version="1.0") as builder:
        builder.add_step("step").param_group("params").add_attribute(
            "temperature", "float", "C", 20
        ).close_group().close_step()

    uow = client.backend.begin()
    try:
        template = (
            client.backend.session.query(ProcessTemplate)
            .filter_by(name="Nested PT")
            .one()
        )
        template.status = LifecycleStatus.ACTIVE
        client.backend.session.flush()
        attribute = (
            client.backend.session.query(AttributeTemplate)
            .filter_by(name="temperature")
            .one()
        )
        attribute.unit = "K"
        with pytest.raises(ValueError, match="active process template"):
            client.backend.session.flush()
    finally:
        uow.rollback()


def test_active_resource_template_rejects_nested_child_mutation(client):
    client.create_campaign("Nested Resource Guard", "P3", None)
    with client.build_resource_template(
        name="Nested RT", type_names=["plate"], version="1.0"
    ) as builder:
        builder.add_child("well", ["sample"]).close_child()

    uow = client.backend.begin()
    try:
        root = (
            client.backend.session.query(ResourceTemplate)
            .filter_by(name="Nested RT")
            .one()
        )
        child = (
            client.backend.session.query(ResourceTemplate).filter_by(name="well").one()
        )
        root.status = LifecycleStatus.ACTIVE
        client.backend.session.flush()
        child.name = "changed"
        with pytest.raises(ValueError, match="active resource template"):
            client.backend.session.flush()
    finally:
        uow.rollback()


def test_first_resource_instance_activates_template(client):
    client.create_campaign("Instance Guard", "P4", None)
    with client.build_resource_template(
        name="Instance RT", type_names=["sample"], version="1.0"
    ):
        pass
    client.create_resource("instance", "Instance RT")

    uow = client.backend.begin()
    try:
        template = (
            client.backend.session.query(ResourceTemplate)
            .filter_by(name="Instance RT")
            .one()
        )
        resource = (
            client.backend.session.query(Resource).filter_by(name="instance").one()
        )
        assert template.status is LifecycleStatus.ACTIVE
        assert resource.status is LifecycleStatus.MUTABLE
    finally:
        uow.rollback()
