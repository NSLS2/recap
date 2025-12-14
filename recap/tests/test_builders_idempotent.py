from recap.dsl.process_builder import ProcessTemplateBuilder
from recap.dsl.resource_builder import ResourceTemplateBuilder
from recap.utils.general import Direction


def test_resource_builder_reuse_same_resource(client):
    # create once
    with ResourceTemplateBuilder(
        name="RB-Template", type_names=["container"], backend=client.backend
    ) as rtb:
        rtb.prop_group("details").add_attribute(
            "serial", "str", "", "abc"
        ).close_group()
    # build resource and mutate props, then reopen builder and mutate again
    with client.build_resource("RB-1", "RB-Template") as rb:
        rb.resource.properties["details"].values["serial"] = "xyz"
    with client.build_resource(resource_id=rb.resource.id) as rb2:
        rb2.resource.properties["details"].values["serial"] = "xyz2"

    refreshed = (
        client.query_maker().resources().filter(name="RB-1").include_template().first()
    )
    assert refreshed.properties.details.values.serial == "xyz2"


def test_resource_template_builder_reuse_same_template(client):
    with ResourceTemplateBuilder(
        name="RTB", type_names=["container"], backend=client.backend
    ) as rtb:
        rtb.prop_group("meta").add_attribute("foo", "str", "", "").close_group()
    # reopen same template by ref and add another attribute
    existing = client.query_maker().resource_templates().filter(name="RTB").first()
    with ResourceTemplateBuilder(
        name="RTB",
        type_names=["container"],
        backend=client.backend,
        resource_template_id=existing.id,
    ) as rtb2:
        rtb2.prop_group("meta").add_attribute("bar", "str", "", "").close_group()

    refreshed = client.query_maker().resource_templates().filter(name="RTB").first()
    fields = {
        a.name for a in refreshed.attribute_group_templates[0].attribute_templates
    }
    assert {"foo", "bar"} == fields


def test_process_builder_reuse_same_run(client):
    with ProcessTemplateBuilder(
        backend=client.backend, name="PTB", version="1.0"
    ) as ptb:
        ptb.add_resource_slot(
            "slot1", "container", Direction.input, create_resource_type=True
        ).add_step("S1").param_group("pg").add_attribute(
            "v", "int", "", 1
        ).close_group().close_step()

    client.create_campaign("C-reuse", "P-reuse")
    with client.build_process_run(
        name="run-reuse",
        description="desc",
        template_name="PTB",
        version="1.0",
    ) as _:
        pass

    run = (
        client.query_maker()
        .process_runs()
        .filter(name="run-reuse")
        .include_steps(include_parameters=True)
        .first()
    )
    # Create a resource that satisfies the slot and assign it
    with client.build_resource_template(
        name="ContainerRT", type_names=["container"]
    ) as _:
        pass
    container_res = client.create_resource("SlotRes", "ContainerRT")

    with client.build_process_run(process_run_id=run.id) as prb2:
        prb2.assign_resource("slot1", container_res)
        params = prb2.get_params("S1")
        params.pg.v = 5
        prb2.set_params(params)

    refreshed = (
        client.query_maker()
        .process_runs()
        .filter(name="run-reuse")
        .include_steps(include_parameters=True)
        .first()
    )
    assert refreshed.steps["S1"].parameters.pg.values.v == 5
