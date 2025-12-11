import pytest


def test_resource_builder_set_model_updates_persisted(client):
    with client.build_resource_template(name="RBM-T", type_names=["container"]) as rtb:
        rtb.prop_group("details").add_attribute(
            "serial", "str", "", "abc"
        ).close_group()

    with client.build_resource("RBM-R", "RBM-T") as rb:
        model = rb.get_model()
        model.properties.details.values.serial = "updated"
        rb.set_model(model)

    refreshed = (
        client.query_maker().resources().filter(name="RBM-R").include_template().first()
    )
    assert refreshed.properties.details.values.serial == "updated"


def test_resource_builder_set_model_rejects_mismatch(client):
    with client.build_resource_template(name="RBM-T2", type_names=["container"]) as rtb:
        rtb.prop_group("details").add_attribute(
            "serial", "str", "", "abc"
        ).close_group()
    res1 = client.create_resource("RBM-R1", "RBM-T2")
    client.create_resource("RBM-R2", "RBM-T2")

    res2_model = (
        client.query_maker()
        .resources()
        .filter(name="RBM-R2")
        .include_template()
        .first()
    )

    def set_mismatched_model():
        with client.build_resource(resource=res1) as rb:
            rb.set_model(res2_model)

    with pytest.raises(ValueError):
        set_mismatched_model()


def test_resource_template_builder_set_model_handles_same_and_mismatch(client):
    with client.build_resource_template(name="RTM-1", type_names=["container"]) as rtb1:
        rtb1.prop_group("g").add_attribute("a", "str", "", "").close_group()
    with client.build_resource_template(name="RTM-2", type_names=["container"]) as rtb2:
        rtb2.prop_group("g").add_attribute("b", "str", "", "").close_group()

    rt1 = client.query_maker().resource_templates().filter(name="RTM-1").first()
    rt2 = client.query_maker().resource_templates().filter(name="RTM-2").first()

    def set_mismatched_model():
        with client.build_resource_template(resource_template=rt1) as builder:
            model = builder.get_model(update=True)
            builder.set_model(model)  # same ID should pass
            builder.set_model(rt2)  # mismatch ID should fail

    with pytest.raises(ValueError):
        set_mismatched_model()


def test_process_template_builder_set_model_handles_mismatch(client):
    with client.build_process_template("PTM-1", "1.0") as ptb1:
        ptb1.add_step("A")
    with client.build_process_template("PTM-2", "1.0") as ptb2:
        ptb2.add_step("B")

    pt1 = client.query_maker().process_templates().filter(name="PTM-1").first()
    pt2 = client.query_maker().process_templates().filter(name="PTM-2").first()

    def set_mismatched_model():
        with client.build_process_template(process_template=pt1) as builder:
            model = builder.get_model(update=True)
            builder.set_model(model)  # same ID ok
            builder.set_model(pt2)

    with pytest.raises(ValueError):
        set_mismatched_model()


def test_process_run_builder_set_model_handles_mismatch(client):
    with client.build_process_template("PTM-R", "1.0") as ptb:
        ptb.add_step("S")

    client.create_campaign("C-M1", "P-M1")
    with client.build_process_run(
        name="RUN-1",
        description="d1",
        template_name="PTM-R",
        version="1.0",
    ) as _:
        pass

    with client.build_process_run(
        name="RUN-2",
        description="d2",
        template_name="PTM-R",
        version="1.0",
    ) as _:
        pass

    run1_model = (
        client.query_maker().process_runs().filter(name="RUN-1").include_steps().first()
    )
    run2_model = (
        client.query_maker().process_runs().filter(name="RUN-2").include_steps().first()
    )

    def set_mismatched_model():
        with client.build_process_run(process_run=run1_model) as builder:
            model = builder.get_model(update=True)
            builder.set_model(model)  # same ID ok
            builder.set_model(run2_model)

    with pytest.raises(ValueError):
        set_mismatched_model()
