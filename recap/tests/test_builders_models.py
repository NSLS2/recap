import pytest


def test_resource_builder_set_model_updates_persisted(client):
    with client.build_resource_template(name="RBM-T", type_names=["container"]) as rtb:
        rtb.prop_group("details").add_attribute(
            "serial", "str", "", "abc"
        ).close_group()

    with client.build_resource("RBM-R", "RBM-T") as rb:
        model = rb.get_model()
        model.properties.details.values.serial.value = "updated"
        rb.set_model(model)
        resource_id = rb.resource.id

    with client.build_resource(resource_id=resource_id) as builder:
        refreshed = builder.get_model(update=True)
    assert refreshed.properties.details.values.serial.value == "updated"


def test_resource_builder_set_model_rejects_mismatch(client):
    with client.build_resource_template(name="RBM-T2", type_names=["container"]) as rtb:
        rtb.prop_group("details").add_attribute(
            "serial", "str", "", "abc"
        ).close_group()
    res1 = client.create_resource("RBM-R1", "RBM-T2")
    res2 = client.create_resource("RBM-R2", "RBM-T2")
    with client.build_resource(resource_id=res2.id) as builder:
        res2_model = builder.get_model(update=True)

    def set_mismatched_model():
        with client.build_resource(resource_id=res1.id) as rb:
            rb.set_model(res2_model)

    with pytest.raises(ValueError):
        set_mismatched_model()


def test_resource_template_builder_set_model_handles_same_and_mismatch(client):
    with client.build_resource_template(name="RTM-1", type_names=["container"]) as rtb1:
        rtb1.prop_group("g").add_attribute("a", "str", "", "").close_group()
    with client.build_resource_template(name="RTM-2", type_names=["container"]) as rtb2:
        rtb2.prop_group("g").add_attribute("b", "str", "", "").close_group()

    rt1 = rtb1.template
    rt2 = rtb2.template

    def set_mismatched_model():
        with client.build_resource_template(resource_template_id=rt1.id) as builder:
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

    pt1 = ptb1.template
    pt2 = ptb2.template

    def set_mismatched_model():
        with client.build_process_template(process_template_id=pt1.id) as builder:
            model = builder.get_model(update=True)
            builder.set_model(model)  # same ID ok
            builder.set_model(pt2)

    with pytest.raises(ValueError):
        set_mismatched_model()


def test_process_run_builder_set_model_handles_mismatch(client):
    with client.build_process_template("PTM-R", "1.0") as ptb:
        ptb.add_step("S")

    with client.build_process_run(
        name="RUN-1",
        description="d1",
        template_name="PTM-R",
        version="1.0",
    ) as builder:
        run1_model = builder.process_run

    with client.build_process_run(
        name="RUN-2",
        description="d2",
        template_name="PTM-R",
        version="1.0",
    ) as builder:
        run2_model = builder.process_run

    def set_mismatched_model():
        with client.build_process_run(process_run_id=run1_model.id) as builder:
            model = builder.get_model(update=True)
            builder.set_model(model)  # same ID ok
            builder.set_model(run2_model)

    with pytest.raises(ValueError):
        set_mismatched_model()


def test_resource_builder_add_child_persists_and_links(client):
    """ResourceBuilder.add_child() creates a child resource in the parent's
    unit of work and links it to the parent. The parent's __exit__ commits both.

    This locks in the instance-level add_child() path, which shares the parent
    builder's session and is otherwise untested.
    """
    with client.build_resource_template(
        name="AC-Parent", type_names=["container"]
    ) as rtb:
        rtb.close_child()
    with client.build_resource_template(
        name="AC-Child", type_names=["container"]
    ) as rtb:
        rtb.close_child()

    with client.build_resource("AC-Root", "AC-Parent") as rb:
        rb.add_child("AC-Leaf", "AC-Child")
        root_id = rb.resource.id

    with client.build_resource(resource_id=root_id) as builder:
        parent = builder.get_model(update=True)
    child = parent.children["AC-Leaf"]

    assert child is not None
    assert child.template.name == "AC-Child"
    assert "AC-Leaf" in parent.children
    assert parent.children["AC-Leaf"].id == child.id
