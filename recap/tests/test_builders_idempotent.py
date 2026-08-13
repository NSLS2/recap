import warnings

import pytest

from recap.commands.models import UpdateResource, UpdateResourceTemplate
from recap.dsl.process_builder import ProcessTemplateBuilder
from recap.dsl.resource_builder import ResourceTemplateBuilder
from recap.exceptions import (
    ExistingProcessRunError,
    ExistingProcessRunWarning,
    ExistingProcessTemplateError,
    ExistingProcessTemplateWarning,
    ExistingResourceError,
    ExistingResourceTemplateError,
    ExistingResourceTemplateWarning,
    ExistingResourceWarning,
)
from recap.utils.general import Direction


def test_resource_builder_reuse_same_resource(client):
    # create once
    with client.build_resource_template(name="RB-Template", type_names=["container"]) as rtb:
        rtb.prop_group("details").add_attribute(
            "serial", "str", "", "abc"
        ).close_group()
    # build resource and mutate props, then reopen builder and mutate again
    with client.build_resource("RB-1", "RB-Template") as rb:
        rb.resource.properties["details"].values["serial"] = "xyz"
    with client.build_resource(resource_id=rb.resource.id) as rb2:
        rb2.resource.properties["details"].values["serial"] = "xyz2"

    with client.build_resource(resource_id=rb.resource.id) as verifier:
        refreshed = verifier.get_model(update=True)
    assert refreshed.properties.details.values.serial.value == "xyz2"


def test_reused_resource_clean_exit_skips_update_and_mutation_updates(client, monkeypatch):
    with client.build_resource_template(
        name="RB-Command-Template", type_names=["container"]
    ) as template:
        template.prop_group("properties").add_attribute(
            "serial", "str", "", ""
        ).close_group()
    resource = client.create_resource("RB-Command-1", "RB-Command-Template")
    commands = []
    execute = client.backend.writer.execute

    def recording_execute(command, context):
        commands.append(command)
        return execute(command, context)

    monkeypatch.setattr(client.backend.writer, "execute", recording_execute)
    with client.build_resource(
        "RB-Command-1", "RB-Command-Template", on_existing="silent"
    ):
        pass
    assert not any(isinstance(command, UpdateResource) for command in commands)

    commands.clear()
    with client.build_resource(
        "RB-Command-1", "RB-Command-Template", on_existing="silent"
    ) as builder:
        builder.resource.properties["properties"].values["serial"] = "mutated"
    assert sum(isinstance(command, UpdateResource) for command in commands) == 1


def test_resource_template_builder_reuse_same_template(client):
    with client.build_resource_template(name="RTB", type_names=["container"]) as rtb:
        rtb.prop_group("meta").add_attribute("foo", "str", "", "").close_group()
    # reopen same template by ref and add another attribute
    existing = rtb.template
    with client.build_resource_template(resource_template_id=existing.id) as rtb2:
        rtb2.prop_group("meta").add_attribute("bar", "str", "", "").close_group()

    refreshed = rtb2.get_model(update=True)
    fields = {
        a.name for a in refreshed.attribute_group_templates[0].attribute_templates
    }
    assert {"foo", "bar"} == fields


def test_reused_resource_template_clean_exit_skips_update_and_mutation_updates(
    client, monkeypatch
):
    with client.build_resource_template(name="RTB-Command", type_names=["container"]) as created:
        pass
    existing = created.template
    commands = []
    execute = client.backend.writer.execute

    def recording_execute(command, context):
        commands.append(command)
        return execute(command, context)

    monkeypatch.setattr(client.backend.writer, "execute", recording_execute)
    with client.build_resource_template(resource_template_id=existing.id):
        pass
    assert not any(
        isinstance(command, UpdateResourceTemplate) for command in commands
    )

    commands.clear()
    with client.build_resource_template(resource_template_id=existing.id) as builder:
        builder.prop_group("new").add_attribute("value", "str", "", "").close_group()
    assert sum(isinstance(command, UpdateResourceTemplate) for command in commands) == 1


def test_process_builder_reuse_same_run(client):
    with client.build_process_template("PTB", "1.0") as ptb:
        ptb.add_resource_slot(
            "slot1", "container", Direction.input, create_resource_type=True
        ).add_step("S1").param_group("pg").add_attribute(
            "v", "int", "", 1
        ).close_group().close_step()

    with client.build_process_run(
        name="run-reuse",
        description="desc",
        template_name="PTB",
        version="1.0",
    ) as created:
        run_id = created.process_run.id
    # Create a resource that satisfies the slot and assign it
    with client.build_resource_template(
        name="ContainerRT", type_names=["container"]
    ) as _:
        pass
    container_res = client.create_resource("SlotRes", "ContainerRT")

    with client.build_process_run(process_run_id=run_id) as prb2:
        prb2.assign_resource("slot1", container_res)
        params = prb2.get_params("S1")
        params.pg.v = 5
        prb2.set_params(params)

    with client.build_process_run(process_run_id=run_id) as verifier:
        refreshed = verifier.process_run
    assert refreshed.steps["S1"].parameters.pg.values.v.value == 5


def test_set_params_persists_after_reopen_by_id(client):
    """Issue #4 regression: set_params via process_run_id must survive commit.

    Scenario:
    1. Create process run by name, commit.
    2. Re-open by process_run_id.
    3. set_params to new values.
    4. Exit context (commit).
    5. Load in a fresh query — values must match what was set.
    """
    with client.build_process_template("PT-I4", "1.0") as ptb:
        ptb.add_resource_slot(
            "src", "container", Direction.input, create_resource_type=True
        ).add_step("Step-A").param_group("grp").add_attribute(
            "x", "int", "", 0
        ).add_attribute("y", "str", "", "init").close_group().close_step()

    # 1. Create process run by name
    with client.build_process_run(
        name="run-i4",
        description="issue4",
        template_name="PT-I4",
        version="1.0",
    ) as created:
        run_id = created.process_run.id

    # Assign required resource
    with client.build_resource_template(
        name="ContainerRT-I4", type_names=["container"]
    ) as _:
        pass
    res = client.create_resource("Res-I4", "ContainerRT-I4")

    # 2-3. Re-open by ID, set_params
    with client.build_process_run(process_run_id=run_id) as prb:
        prb.assign_resource("src", res)
        params = prb.get_params("Step-A")
        params.grp.x = 42
        params.grp.y = "updated"
        prb.set_params(params)
    # 4. Context exited → committed

    # 5. Verify persistence in fresh query
    with client.build_process_run(process_run_id=run_id) as verifier:
        fresh = verifier.process_run
    assert fresh.steps["Step-A"].parameters.grp.values.x.value == 42
    assert fresh.steps["Step-A"].parameters.grp.values.y.value == "updated"


def test_resource_template_builder_reuses_existing_with_warning(client):
    with client.build_resource_template(name="ReuseRT", type_names=["container"]) as _:
        pass

    with (
        pytest.warns(ExistingResourceTemplateWarning, match="bump the version"),
        client.build_resource_template(
            name="ReuseRT", type_names=["container"]
        ) as builder,
    ):
        assert builder.template.name == "ReuseRT"


def test_resource_template_builder_on_existing_raise_raises(client):
    with client.build_resource_template(name="StrictRT", type_names=["container"]) as _:
        pass

    with (
        pytest.raises(ExistingResourceTemplateError),
        client.build_resource_template(
            name="StrictRT",
            type_names=["container"],
            on_existing="raise",
        ),
    ):
        pass


def test_process_template_builder_reuses_existing_with_warning(client):
    with client.build_process_template("ReusePT", "1.0") as ptb:
        ptb.add_step("step-1")

    with (
        pytest.warns(ExistingProcessTemplateWarning, match="bump the version"),
        client.build_process_template("ReusePT", "1.0") as ptb,
    ):
        ptb.add_step("step-2")

    with client.build_process_template("ReusePT", "1.0", on_existing="silent") as ptb:
        refreshed = ptb.get_model(update=True)
    assert refreshed is not None


def test_process_template_builder_on_existing_raise_raises(client):
    with client.build_process_template("StrictPT", "1.0") as ptb:
        ptb.add_step("step-1")

    with (
        pytest.raises(ExistingProcessTemplateError, match="already exists"),
        client.build_process_template(
            "StrictPT",
            "1.0",
            on_existing="raise",
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
        pytest.warns(ExistingResourceWarning, match="will be reused"),
        client.build_resource("ReuseRes", "ReuseResTemplate") as rb,
    ):
        assert rb.resource.id == first.id


def test_resource_builder_on_existing_raise_raises(client):
    with client.build_resource_template(
        name="StrictResTemplate", type_names=["container"]
    ) as _:
        pass

    client.create_resource("StrictRes", "StrictResTemplate")
    with (
        pytest.raises(ExistingResourceError),
        client.build_resource(
            "StrictRes",
            "StrictResTemplate",
            on_existing="raise",
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

    with client.build_process_run("ReuseRun", "desc", "ReuseRunPT", "1.0") as _:
        pass

    with (
        pytest.warns(ExistingProcessRunWarning, match="will be reused"),
        client.build_process_run("ReuseRun", "desc", "ReuseRunPT", "1.0") as prb,
    ):
        assert prb.process_run.name == "ReuseRun"
        assert [step.name for step in prb._steps] == ["S1"]


def test_process_run_builder_on_existing_raise_raises(client):
    with client.build_process_template("StrictRunPT", "1.0") as ptb:
        ptb.add_step("S1")

    with client.build_process_run("StrictRun", "desc", "StrictRunPT", "1.0") as _:
        pass

    with (
        pytest.raises(ExistingProcessRunError),
        client.build_process_run(
            "StrictRun",
            "desc",
            "StrictRunPT",
            "1.0",
            on_existing="raise",
        ),
    ):
        pass


def test_resource_template_builder_on_existing_silent_suppresses_warning(client):
    with client.build_resource_template(name="SilentRT", type_names=["container"]) as _:
        pass

    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        with client.build_resource_template(
            name="SilentRT", type_names=["container"], on_existing="silent"
        ) as builder:
            assert builder.template.name == "SilentRT"
    assert not [
        w for w in record if issubclass(w.category, ExistingResourceTemplateWarning)
    ]


def test_process_template_silent_reuse_same_steps_idempotent(client):
    """Re-registering the exact same template structure with
    on_existing='silent' must be a no-op — no IntegrityError, no duplicates.
    """

    def register_template():
        with client.build_process_template(
            "IdempotPT", "1.0", on_existing="silent"
        ) as ptb:
            step = ptb.add_step("Collect")
            step.add_parameters(
                {"Exposure": [{"name": "dwell", "type": "int", "default": 5}]}
            )
            ptb.add_resource_slot(
                "sample", "container", Direction.input, create_resource_type=True
            )

    # First registration
    register_template()

    # Second registration — must not raise
    register_template()

    with client.build_process_template("IdempotPT", "1.0", on_existing="silent") as ptb:
        refreshed = ptb.get_model(update=True)
    assert refreshed is not None
    step_names = list(refreshed.step_templates.keys())
    assert step_names == ["Collect"]
    # Verify only one slot exists
    slot_names = [s.name for s in refreshed.resource_slots]
    assert slot_names == ["sample"]


def test_process_template_silent_reuse_adds_new_step(client):
    """Re-registering with on_existing='silent' and adding a NEW step to an
    existing template should succeed.
    """
    with client.build_process_template("GrowPT", "1.0", on_existing="silent") as ptb:
        ptb.add_step("Step1")

    with client.build_process_template("GrowPT", "1.0", on_existing="silent") as ptb:
        ptb.add_step("Step1")  # existing — idempotent
        ptb.add_step("Step2")  # new step

    with client.build_process_template("GrowPT", "1.0", on_existing="silent") as ptb:
        refreshed = ptb.get_model(update=True)
    step_names = sorted(refreshed.step_templates.keys())
    assert step_names == ["Step1", "Step2"]


def test_process_template_silent_reuse_bind_slot_idempotent(client):
    """Re-running bind_slot with the same role/slot combo is a no-op."""
    with client.build_process_template("BindPT", "1.0", on_existing="silent") as ptb:
        ptb.add_resource_slot(
            "data", "container", Direction.input, create_resource_type=True
        )
        step = ptb.add_step("Process")
        step.bind_slot("input_data", "data")

    # Re-register — same bind_slot calls
    with client.build_process_template("BindPT", "1.0", on_existing="silent") as ptb:
        ptb.add_resource_slot(
            "data", "container", Direction.input, create_resource_type=True
        )
        step = ptb.add_step("Process")
        step.bind_slot("input_data", "data")

    with client.build_process_template("BindPT", "1.0", on_existing="silent") as ptb:
        refreshed = ptb.get_model(update=True)
    assert refreshed is not None


def test_slot_conflict_or_predicate(client):
    """add_resource_slot should raise if an existing slot has a different
    type OR direction — not only when both differ.
    """
    with client.build_process_template(
        "ConflictPT", "1.0", on_existing="silent"
    ) as ptb:
        ptb.add_resource_slot(
            "data", "container", Direction.input, create_resource_type=True
        )

    # Same name, same type, different direction → should raise
    with (
        pytest.raises(ValueError, match="different type/direction"),
        client.build_process_template("ConflictPT", "1.0", on_existing="silent") as ptb,
    ):
        ptb.add_resource_slot(
            "data", "container", Direction.output, create_resource_type=True
        )


# ---------------------------------------------------------------------------
# Issue #1: on_existing semantics for resources
# ---------------------------------------------------------------------------


def _make_simple_template(client, name="OnExist-T"):
    """Helper: create a minimal resource template for on_existing tests."""
    with client.build_resource_template(name=name, type_names=["container"]) as rtb:
        rtb.prop_group("info").add_attribute("val", "str", "", "default").close_group()
    return rtb


def test_on_existing_create_allows_duplicates(client):
    """on_existing='create' (default for create_resource) always inserts."""
    _make_simple_template(client)
    r1 = client.create_resource("Dup", "OnExist-T", on_existing="create")
    r2 = client.create_resource("Dup", "OnExist-T", on_existing="create")
    assert r1.id != r2.id
    assert r1.name == r2.name == "Dup"


def test_on_existing_silent_reuses(client):
    """on_existing='silent' reuses without warning or error."""
    _make_simple_template(client)
    r1 = client.create_resource("SilentR", "OnExist-T", on_existing="create")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        r2 = client.create_resource("SilentR", "OnExist-T", on_existing="silent")
    assert r1.id == r2.id
    resource_warns = [
        w for w in caught if issubclass(w.category, ExistingResourceWarning)
    ]
    assert len(resource_warns) == 0


def test_on_existing_warn_reuses_with_warning(client):
    """on_existing='warn' reuses and emits ExistingResourceWarning."""
    _make_simple_template(client)
    r1 = client.create_resource("WarnR", "OnExist-T", on_existing="create")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        r2 = client.create_resource("WarnR", "OnExist-T", on_existing="warn")
    assert r1.id == r2.id
    resource_warns = [
        w for w in caught if issubclass(w.category, ExistingResourceWarning)
    ]
    assert len(resource_warns) == 1
    assert "already exists" in str(resource_warns[0].message)


def test_on_existing_raise_raises(client):
    """on_existing='raise' raises ExistingResourceError."""
    _make_simple_template(client)
    client.create_resource("RaiseR", "OnExist-T", on_existing="create")
    with pytest.raises(ExistingResourceError, match="already exists"):
        client.create_resource("RaiseR", "OnExist-T", on_existing="raise")


def test_on_existing_no_match_creates(client):
    """When no existing resource matches, any on_existing mode creates."""
    _make_simple_template(client)
    r1 = client.create_resource("UniqueR", "OnExist-T", on_existing="silent")
    assert r1.name == "UniqueR"


def test_build_resource_on_existing_warn_default(client):
    """build_resource defaults to on_existing='warn' and reuses."""
    _make_simple_template(client)
    with client.build_resource("BR-Warn", "OnExist-T") as rb1:
        pass
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with client.build_resource("BR-Warn", "OnExist-T") as rb2:
            pass
    assert rb1.resource.id == rb2.resource.id
    resource_warns = [
        w for w in caught if issubclass(w.category, ExistingResourceWarning)
    ]
    assert len(resource_warns) == 1


def test_build_resource_on_existing_create_allows_duplicate(client):
    """build_resource with on_existing='create' inserts a second resource."""
    _make_simple_template(client)
    with client.build_resource("BR-Dup", "OnExist-T", on_existing="create") as rb1:
        pass
    with client.build_resource("BR-Dup", "OnExist-T", on_existing="create") as rb2:
        pass
    assert rb1.resource.id != rb2.resource.id


# ---------------------------------------------------------------------------
# Issue #3: build_resource with parent parameter
# ---------------------------------------------------------------------------


def test_build_resource_with_parent_schema(client):
    """build_resource(parent=ResourceSchema) creates a child resource."""
    _make_simple_template(client, name="ParentT")
    _make_simple_template(client, name="ChildT")
    parent = client.create_resource("Parent-1", "ParentT", on_existing="create")
    with client.build_resource(
        "Child-1", "ChildT", parent=parent, on_existing="create"
    ):
        pass
    # Verify child is linked
    with client.build_resource(resource_id=parent.id) as parent_builder:
        refreshed = parent_builder.get_model(update=True)
    assert "Child-1" in refreshed.children


def test_build_resource_with_parent_uuid(client):
    """build_resource(parent=UUID) resolves the parent and creates a child."""
    _make_simple_template(client, name="ParentT2")
    _make_simple_template(client, name="ChildT2")
    parent = client.create_resource("Parent-2", "ParentT2", on_existing="create")
    with client.build_resource(
        "Child-2", "ChildT2", parent=parent.id, on_existing="create"
    ):
        pass
    with client.build_resource(resource_id=parent.id) as parent_builder:
        refreshed = parent_builder.get_model(update=True)
    assert "Child-2" in refreshed.children


def test_build_resource_resource_id_with_parent_raises(client):
    """Combining resource_id and parent raises TypeError."""
    from uuid import uuid4

    with pytest.raises(TypeError, match="Cannot combine resource_id with parent"):
        client.build_resource(resource_id=uuid4(), parent=uuid4())


# ---------------------------------------------------------------------------
# Issue #5: Optional resource slots (required=False)
# ---------------------------------------------------------------------------


def test_optional_output_slot_does_not_block_get_params(client):
    """get_params succeeds when only a required input slot is assigned,
    even though an optional output slot is unassigned."""
    with client.build_process_template("PT-Opt", "1.0") as pt:
        pt.add_resource_slot(
            "input_data", "container", Direction.input, create_resource_type=True
        )
        pt.add_resource_slot(
            "output_result",
            "container",
            Direction.output,
            create_resource_type=True,
            required=False,
        )
        (
            pt.add_step("Compute")
            .param_group("cfg")
            .add_attribute("iterations", "int", "", 10)
            .close_group()
            .bind_slot("data_in", "input_data")
            .close_step()
        )

    with client.build_resource_template(
        name="ContainerRT-Opt", type_names=["container"]
    ) as _:
        pass
    input_res = client.create_resource("Input-1", "ContainerRT-Opt")

    with client.build_process_run(
        name="run-opt",
        description="optional slot test",
        template_name="PT-Opt",
        version="1.0",
    ) as prb:
        prb.assign_resource("input_data", input_res)
        # This should NOT raise even though output_result is unassigned
        params = prb.get_params("Compute")
        assert params.cfg.iterations.value == 10


def test_all_required_slots_preserves_old_behavior(client):
    """When all slots are required (default), missing assignments still raise."""
    with client.build_process_template("PT-AllReq", "1.0") as pt:
        pt.add_resource_slot(
            "slot_a", "container", Direction.input, create_resource_type=True
        )
        pt.add_resource_slot(
            "slot_b", "container", Direction.output, create_resource_type=True
        )
        (
            pt.add_step("DoWork")
            .param_group("p")
            .add_attribute("val", "int", "", 0)
            .close_group()
            .bind_slot("in", "slot_a")
            .close_step()
        )

    with client.build_resource_template(
        name="ContainerRT-AllReq", type_names=["container"]
    ) as _:
        pass
    res_a = client.create_resource("ResA", "ContainerRT-AllReq")

    with (
        pytest.raises(ValueError, match="missing resources for slots"),
        client.build_process_run(
            name="run-allreq",
            description="all required",
            template_name="PT-AllReq",
            version="1.0",
        ) as prb,
    ):
        prb.assign_resource("slot_a", res_a)
        # slot_b is required but unassigned → should raise
        prb.get_params("DoWork")


def test_optional_slot_can_be_assigned_later(client):
    """An optional slot can be assigned without error."""
    with client.build_process_template("PT-Late", "1.0") as pt:
        pt.add_resource_slot(
            "src", "container", Direction.input, create_resource_type=True
        )
        pt.add_resource_slot(
            "dest",
            "container",
            Direction.output,
            create_resource_type=True,
            required=False,
        )
        (
            pt.add_step("Move")
            .param_group("mv")
            .add_attribute("count", "int", "", 0)
            .close_group()
            .bind_slot("from", "src")
            .bind_slot("to", "dest")
            .close_step()
        )

    with client.build_resource_template(
        name="ContainerRT-Late", type_names=["container"]
    ) as _:
        pass
    src_res = client.create_resource("Src", "ContainerRT-Late")
    dest_res = client.create_resource("Dest", "ContainerRT-Late")

    # First: create run with only required slot
    with client.build_process_run(
        name="run-late",
        description="late assign",
        template_name="PT-Late",
        version="1.0",
    ) as prb:
        prb.assign_resource("src", src_res)
        params = prb.get_params("Move")
        assert params.mv.count.value == 0
        run_id = prb.process_run.id

    # Re-open and assign optional slot too
    with client.build_process_run(process_run_id=run_id) as prb2:
        prb2.assign_resource("dest", dest_res)
        params = prb2.get_params("Move")
        params.mv.count = 42
        prb2.set_params(params)

    with client.build_process_run(process_run_id=run_id) as verifier:
        fresh = verifier.process_run
    assert fresh.steps["Move"].parameters.mv.values.count.value == 42
