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


def test_resource_template_builder_set_model_persists_detached_edits(client):
    with client.build_resource_template(name="RTM-edit", type_names=["container"]) as builder:
        builder.prop_group("details").add_attribute("serial", "str", "", "old").close_group()
        builder.save()
        template_id = builder.template.id

    with client.build_resource_template(resource_template_id=template_id) as builder:
        model = builder.get_model(update=True)
        model.name = "RTM-edited"
        model.attribute_group_templates[0].attribute_templates[0].default_value = "new"
        builder.set_model(model)

    with client.build_resource_template(resource_template_id=template_id) as builder:
        refreshed = builder.get_model(update=True)
    assert refreshed.name == "RTM-edited"
    assert refreshed.attribute_group_templates[0].attribute_templates[0].default_value == "new"


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


def test_process_template_builder_set_model_persists_detached_edits(client):
    with client.build_process_template("PTM-edit", "1.0") as builder:
        builder.add_step("A").param_group("Inputs").add_attribute("Voltage", "int", "", 0).close_group()
        builder.save()
        template_id = builder.template.id

    with client.build_process_template(process_template_id=template_id) as builder:
        model = builder.get_model(update=True)
        model.name = "PTM-edited"
        model.step_templates["A"].attribute_group_templates[0].attribute_templates[0].default_value = 42
        builder.set_model(model)

    with client.build_process_template(process_template_id=template_id) as builder:
        refreshed = builder.get_model(update=True)
    assert refreshed.name == "PTM-edited"
    assert refreshed.step_templates["A"].attribute_group_templates[0].attribute_templates[0].default_value == 42


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


def test_resource_builder_add_child_persists_and_links(client, recwarn):
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
    assert not [warning for warning in recwarn if warning.category.__name__ == "SAWarning"]


def test_resource_builder_add_child_copies_uuid_source(client):
    with client.build_resource_template(name="AC-UUID-Group", type_names=["group"]):
        pass
    with client.build_resource_template(name="AC-UUID-Leaf", type_names=["leaf"]):
        pass

    group = client.create_resource("AC-UUID-Group-1", "AC-UUID-Group")
    source = client.create_resource("AC-UUID-Source", "AC-UUID-Leaf")

    with client.build_resource(resource_id=group.id) as builder:
        child_builder = builder.add_child(source.id)
        copied_id = child_builder.resource.id

        assert child_builder.parent is builder
        assert copied_id != source.id
        assert child_builder.resource.copied_from_id == source.id
        assert child_builder.resource.template.id == source.template.id

    with client.build_resource(resource_id=group.id) as builder:
        refreshed = builder.get_model(update=True)
    assert refreshed.children[source.name].id == copied_id
    assert refreshed.children[source.name].copied_from_id == source.id


def test_resource_builder_add_child_copies_resource_schema_source(client):
    with client.build_resource_template(name="AC-Schema-Group", type_names=["group"]):
        pass
    with client.build_resource_template(name="AC-Schema-Leaf", type_names=["leaf"]):
        pass

    group = client.create_resource("AC-Schema-Group-1", "AC-Schema-Group")
    source = client.create_resource("AC-Schema-Source", "AC-Schema-Leaf")

    with client.build_resource(resource_id=group.id) as builder:
        child_builder = builder.add_child(source)

        assert child_builder.parent is builder
        assert child_builder.resource.id != source.id
        assert child_builder.resource.copied_from_id == source.id
        assert child_builder.resource.template.id == source.template.id

    with client.build_resource(resource_id=group.id) as builder:
        refreshed = builder.get_model(update=True)
    assert refreshed.children[source.name].id == child_builder.resource.id
    assert refreshed.children[source.name].copied_from_id == source.id


def test_new_process_run_keeps_provisional_id_after_context_commit(client):
    with client.build_process_template("ID PT", "1.0"):
        pass
    builder = client.build_process_run("ID run", "", "ID PT", "1.0")
    provisional_id = builder.process_run.id
    with builder:
        pass
    assert builder.process_run.id == provisional_id


def test_new_resource_keeps_provisional_id_after_context_commit(client):
    with client.build_resource_template(name="ID RT", type_names=["sample"]):
        pass
    builder = client.build_resource("ID resource", "ID RT")
    provisional_id = builder.resource.id
    with builder:
        pass
    assert builder.resource.id == provisional_id


@pytest.mark.parametrize("kind", ["process", "resource"])
def test_new_template_keeps_provisional_id_after_context_commit(client, kind):
    builder = (
        client.build_process_template("ID PT", "1.0")
        if kind == "process"
        else client.build_resource_template(name="ID RT", type_names=["sample"])
    )
    provisional_id = builder.template.id
    with builder:
        pass
    assert builder.template.id == provisional_id
