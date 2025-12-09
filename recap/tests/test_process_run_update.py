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
        step = run.steps[0]

        # mutate typed param values and persist
        step.parameters["Inputs"].values.voltage = 42
        run.update()

    refreshed_run = (
        client.query_maker()
        .process_runs()
        .include_steps(include_parameters=True)
        .filter(id=run.id)
        .first()
    )
    assert refreshed_run is not None
    assert refreshed_run.steps[0].parameters["Inputs"].values.voltage == 42


def test_resource_save_persists_property_changes(client):
    with client.build_resource_template("Robot", ["instrument"]) as rtb:
        rtb.prop_group("Details").add_attribute(
            "serial", "str", "", "abc"
        ).close_group()

    resource = client.create_resource("R1", "Robot")

    resource.properties["Details"].values.serial = "xyz"
    resource.save()

    refreshed = (
        client.query_maker()
        .resources()
        .filter(name="R1", template__name="Robot")
        .first()
    )
    assert refreshed is not None
    assert refreshed.properties["Details"].values.serial == "xyz"
