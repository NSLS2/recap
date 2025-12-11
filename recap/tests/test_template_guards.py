import pytest

from recap.db.process import Direction, ProcessTemplate
from recap.db.resource import ResourceTemplate


def test_resource_template_guard_prevents_updates_when_resources_exist(client):
    with client.build_resource_template(
        name="GuardedTemplate", type_names=["sample"], version="1.0"
    ):
        pass

    with client.build_resource("Res1", "GuardedTemplate") as rb:
        resource = rb.resource

    tmpl_schema = (
        client.query_maker()
        .resource_templates()
        .filter(name="GuardedTemplate", version="1.0")
        .first()
    )

    uow = client.backend.begin()
    try:
        tmpl_model = client.backend.session.get(ResourceTemplate, tmpl_schema.id)
        tmpl_model.name = "ShouldFail"
        with pytest.raises(
            ValueError, match="resource template that already has resources"
        ):
            client.backend.session.flush()
    finally:
        uow.rollback()

    # Ensure the original resource is still intact
    fetched = (
        client.query_maker()
        .resources()
        .filter(name=resource.name)
        .include_template()
        .first()
    )
    assert fetched is not None
    assert fetched.template.name == "GuardedTemplate"


def test_process_template_guard_prevents_updates_when_runs_exist(client):
    with client.build_resource_template(
        name="ProcRes", type_names=["container"], version="1.0"
    ):
        pass
    resource = client.create_resource("ProcRes1", "ProcRes")

    client.create_campaign("Camp", "P1", "SAF1")

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

    pt_schema = (
        client.query_maker()
        .process_runs()
        .filter(name="RunGuard")
        .include_steps()
        .first()
        .template
    )

    uow = client.backend.begin()
    try:
        pt_model = client.backend.session.get(ProcessTemplate, pt_schema.id)
        pt_model.name = "ShouldFail"
        with pytest.raises(ValueError, match="process template with existing runs"):
            client.backend.session.flush()
    finally:
        uow.rollback()

    unchanged = (
        client.query_maker()
        .process_runs()
        .filter(name="RunGuard")
        .include_steps()
        .first()
        .template
    )
    assert unchanged.name == "PT Guard"
