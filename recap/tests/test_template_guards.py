import pytest

from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.db.process import ProcessTemplate
from recap.db.resource import Resource, ResourceTemplate
from recap.db.step import StepTemplate
from recap.lifecycle import LifecycleStatus
from recap.utils.general import Direction


def test_resource_template_guard_prevents_updates_when_resources_exist(client):
    client.create_namespace("resource-guard")
    with client.build_resource_template(
        name="GuardedTemplate", type_names=["sample"], version="1.0"
    ):
        pass

    with client.build_resource("Res1", "GuardedTemplate") as rb:
        resource = rb.resource

    with client._sessionmaker.begin() as session:
        tmpl_model = (
            session.query(ResourceTemplate)
            .filter_by(name="GuardedTemplate", version="1.0")
            .one()
        )
        tmpl_model.name = "ShouldFail"
        with pytest.raises(ValueError, match="active resource template"):
            session.flush()
        session.rollback()

    # Ensure the original resource is still intact
    with client._sessionmaker.begin() as session:
        fetched = session.query(Resource).filter_by(name=resource.name).one()
        template_name = fetched.template.name
    assert fetched is not None
    assert template_name == "GuardedTemplate"


def test_process_template_guard_prevents_updates_when_runs_exist(client):
    client.create_namespace("camp")
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

    with client._sessionmaker.begin() as session:
        pt_model = (
            session.query(ProcessTemplate)
            .filter_by(name="PT Guard", version="1.0")
            .one()
        )
        pt_model.name = "ShouldFail"
        with pytest.raises(ValueError, match="active process template"):
            session.flush()
        session.rollback()

    with client._sessionmaker.begin() as session:
        unchanged = session.query(ProcessTemplate).filter_by(name="PT Guard", version="1.0").one()
    assert unchanged.name == "PT Guard"


def test_active_process_template_rejects_nested_attribute_mutation(client):
    client.create_namespace("nested-guard")
    with client.build_process_template(name="Nested PT", version="1.0") as builder:
        builder.add_step("step").param_group("params").add_attribute(
            "temperature", "float", "C", 20
        ).close_group().close_step()

    with client._sessionmaker.begin() as session:
        template = (
            session.query(ProcessTemplate)
            .filter_by(name="Nested PT")
            .one()
        )
        template.status = LifecycleStatus.ACTIVE
        session.flush()
        attribute = (
            session.query(AttributeTemplate)
            .join(AttributeTemplate.attribute_group_template)
            .join(AttributeGroupTemplate.step_template)
            .filter(AttributeTemplate.name == "temperature")
            .filter(StepTemplate.process_template_id == template.id)
            .one()
        )
        attribute.unit = "K"
        with pytest.raises(ValueError, match="active process template"):
            session.flush()
        session.rollback()


def test_active_resource_template_rejects_nested_child_mutation(client):
    client.create_namespace("nested-resource-guard")
    with client.build_resource_template(
        name="Nested RT", type_names=["plate"], version="1.0"
    ) as builder:
        builder.add_child("well", ["sample"]).close_child()

    with client._sessionmaker.begin() as session:
        root = (
            session.query(ResourceTemplate)
            .filter_by(name="Nested RT")
            .one()
        )
        child = (
            session.query(ResourceTemplate).filter_by(name="well").one()
        )
        root.status = LifecycleStatus.ACTIVE
        session.flush()
        child.name = "changed"
        with pytest.raises(ValueError, match="active resource template"):
            session.flush()
        session.rollback()


def test_first_resource_instance_activates_template(client):
    client.create_namespace("instance-guard")
    with client.build_resource_template(
        name="Instance RT", type_names=["sample"], version="1.0"
    ):
        pass
    client.create_resource("instance", "Instance RT")

    with client._sessionmaker.begin() as session:
        template = (
            session.query(ResourceTemplate)
            .filter_by(name="Instance RT")
            .one()
        )
        resource = (
            session.query(Resource).filter_by(name="instance").one()
        )
        assert template.status is LifecycleStatus.ACTIVE
        assert resource.status is LifecycleStatus.MUTABLE
