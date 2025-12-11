def test_process_run_update_persists_param_changes(client):
    client.create_campaign("Campaign", "proposal-1", saf=None)

    with client.build_process_template("PT-update", "1.0") as ptb:
        (
            ptb.add_step("Mix")
            .param_group("Inputs")
            .add_attribute("Voltage", "int", "", "0")
            .close_group()
            .close_step()
        )

    with client.build_process_run(
        name="run-update",
        description="desc",
        template_name="PT-update",
        version="1.0",
    ) as prb:
        run = prb.process_run
        step = run.steps["Mix"]

        # mutate typed param values and persist
        step.parameters.inputs.values.voltage = 42

    refreshed_run = (
        client.query_maker()
        .process_runs()
        .include_steps(include_parameters=True)
        .filter(id=run.id)
        .first()
    )
    assert refreshed_run is not None
    assert refreshed_run.steps["Mix"].parameters.inputs.values.voltage == 42


def test_resource_builder_persists_property_changes(client):
    with client.build_resource_template(name="Robot", type_names=["instrument"]) as rtb:
        rtb.prop_group("Details").add_attribute(
            "serial", "str", "", "abc"
        ).close_group()

    with client.build_resource("R1", "Robot") as rb:
        rb.resource.properties.details.values.serial = "xyz"

    refreshed = client.query_maker().resources().filter(name="R1").first()
    assert refreshed is not None
    assert refreshed.properties.details.values.serial == "xyz"
