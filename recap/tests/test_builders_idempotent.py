import pytest
from sqlalchemy.exc import IntegrityError

from recap.db.exceptions import ValidationError
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
    assert refreshed.properties.details.values.serial.value == "xyz2"


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
    assert refreshed.steps["S1"].parameters.pg.values.v.value == 5


def test_resource_template_builder_reuses_existing_with_warning(client):
    with client.build_resource_template(name="ReuseRT", type_names=["container"]) as _:
        pass

    with (
        pytest.warns(UserWarning, match="bump the version"),
        client.build_resource_template(
            name="ReuseRT", type_names=["container"]
        ) as builder,
    ):
        assert builder.template.name == "ReuseRT"


def test_resource_template_builder_strict_checking_raises(client):
    with client.build_resource_template(name="StrictRT", type_names=["container"]) as _:
        pass

    with (
        pytest.raises(IntegrityError),
        client.build_resource_template(
            name="StrictRT",
            type_names=["container"],
            strict_checking=True,
        ),
    ):
        pass


def test_process_template_builder_reuses_existing_with_warning(client):
    with client.build_process_template("ReusePT", "1.0") as ptb:
        ptb.add_step("step-1")

    with (
        pytest.warns(UserWarning, match="bump the version"),
        client.build_process_template("ReusePT", "1.0") as ptb,
    ):
        ptb.add_step("step-2")

    refreshed = client.query_maker().process_templates().filter(name="ReusePT").first()
    assert refreshed is not None


def test_process_template_builder_strict_checking_raises(client):
    with client.build_process_template("StrictPT", "1.0") as ptb:
        ptb.add_step("step-1")

    with (
        pytest.raises(ValueError, match="already exists"),
        client.build_process_template(
            "StrictPT",
            "1.0",
            strict_checking=True,
        ) as ptb,
    ):
        ptb.add_step("step-2")


def test_resource_builder_reuses_existing_with_warning(client):
    with client.build_resource_template(
        name="ReuseResTemplate", type_names=["container"]
    ) as _:
        pass

    first = client.create_resource("ReuseRes", "ReuseResTemplate")
    with (
        pytest.warns(UserWarning, match="will be reused"),
        client.build_resource("ReuseRes", "ReuseResTemplate") as rb,
    ):
        assert rb.resource.id == first.id


def test_resource_builder_strict_checking_raises(client):
    with client.build_resource_template(
        name="StrictResTemplate", type_names=["container"]
    ) as _:
        pass

    client.create_resource("StrictRes", "StrictResTemplate")
    with (
        pytest.raises(ValidationError),
        client.build_resource(
            "StrictRes",
            "StrictResTemplate",
            strict_checking=True,
        ),
    ):
        pass


def test_process_run_builder_reuses_existing_with_warning(client):
    with client.build_resource_template(
        name="ReuseRunRT", type_names=["container"]
    ) as _:
        pass
    with client.build_process_template("ReuseRunPT", "1.0") as ptb:
        ptb.add_resource_slot(
            "slot1", "container", Direction.input, create_resource_type=True
        ).add_step("S1")

    client.create_campaign("ReuseCamp", "ReuseProposal")
    with client.build_process_run("ReuseRun", "desc", "ReuseRunPT", "1.0") as _:
        pass

    with (
        pytest.warns(UserWarning, match="will be reused"),
        client.build_process_run("ReuseRun", "desc", "ReuseRunPT", "1.0") as prb,
    ):
        assert prb.process_run.name == "ReuseRun"


def test_process_run_builder_strict_checking_raises(client):
    with client.build_process_template("StrictRunPT", "1.0") as ptb:
        ptb.add_step("S1")

    client.create_campaign("StrictCamp", "StrictProposal")
    with client.build_process_run("StrictRun", "desc", "StrictRunPT", "1.0") as _:
        pass

    with (
        pytest.raises(IntegrityError),
        client.build_process_run(
            "StrictRun",
            "desc",
            "StrictRunPT",
            "1.0",
            strict_checking=True,
        ),
    ):
        pass
