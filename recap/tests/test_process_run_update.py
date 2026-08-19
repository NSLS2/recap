def test_process_run_update_persists_param_changes(client):
    client.create_namespace("process-run-update")

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
        run_id = prb.process_run.id
        canonical_run = prb._process_run

        params = prb.get_params("Mix")
        params.inputs.values.voltage.value = 42
        assert params.inputs.values.voltage.value == 42
        prb.set_params(params)
    with client.build_process_run(process_run_id=run_id) as builder:
        refreshed_run = builder.process_run
    assert refreshed_run is not None
    assert refreshed_run.steps["Mix"].parameters.inputs.values.voltage.value == 42
    assert canonical_run.steps["Mix"].parameters.inputs.values.voltage.value == 42


def test_finalized_process_run_builder_uses_copy_on_write(client):
    client.create_namespace("process-run-finalized-copy")
    with client.build_process_template("PT-finalized-copy", "1.0"):
        pass
    with client.build_process_run(
        name="run-finalized-copy",
        description="desc",
        template_name="PT-finalized-copy",
        version="1.0",
    ) as builder:
        run_id = builder.process_run.id
        builder.finalize()

    with client.build_process_run(process_run_id=run_id) as builder:
        model = builder.get_model()
        model.description = "draft-only"
        assert builder.process_run.description != "draft-only"


def test_finalized_process_run_save_creates_lineage_copy(client):
    client.create_namespace("process-run-copy-lineage")
    with client.build_process_template("PT-copy-lineage", "1.0"):
        pass
    with client.build_process_run(
        name="run-copy-lineage",
        description="source",
        template_name="PT-copy-lineage",
        version="1.0",
    ) as builder:
        source_id = builder.process_run.id
        builder.finalize()

    with client.build_process_run(process_run_id=source_id) as builder:
        draft = builder.get_model()
        draft.description = "copy"
        builder.set_model(draft)

    copied = builder.process_run
    assert copied.id != source_id
    assert copied.copied_from_id == source_id
    assert copied.description == "copy"
    with client.build_process_run(process_run_id=source_id) as source:
        assert source.process_run.description == "source"


def test_resource_builder_persists_property_changes(client):
    with client.build_resource_template(name="Robot", type_names=["instrument"]) as rtb:
        rtb.prop_group("Details").add_attribute(
            "serial", "str", "", "abc"
        ).close_group()

    with client.build_resource("R1", "Robot") as rb:
        rb.resource.properties.details.values.serial.value = "xyz"
        resource_id = rb.resource.id

    with client.build_resource(resource_id=resource_id) as builder:
        refreshed = builder.get_model(update=True)
    assert refreshed is not None
    assert refreshed.properties.details.values.serial.value == "xyz"
