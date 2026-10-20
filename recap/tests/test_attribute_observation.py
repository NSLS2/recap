from datetime import timedelta

import pytest
from pydantic import ValidationError

from recap.utils import dsl

from .transport_factories import STAMP, full_process_run, full_resource


@pytest.mark.parametrize("domain", ["properties", "parameters"])
def test_attribute_assignments_notify_group_and_owner(domain):
    owner = (
        full_resource()
        if domain == "properties"
        else full_process_run().steps["Acquire"]
    )
    dsl.bind_attribute_observers(owner)
    groups = getattr(owner, domain)
    group = next(iter(groups.values()))
    assert group.changed_attributes == {}
    assert not getattr(owner, f"{domain}_dirty")

    group["Captured At"] = STAMP + timedelta(seconds=1)
    group["Captured At"].unit = "s"

    assert group.changed_attributes == {"Captured At": {"value", "unit"}}
    assert getattr(owner, f"{domain}_dirty")


def test_rejected_attribute_assignment_does_not_notify():
    owner = full_process_run().steps["Acquire"]
    dsl.bind_attribute_observers(owner)
    group = owner.parameters.acquisition
    with pytest.raises(ValidationError):
        group.captured_at.value = "invalid datetime"
    assert group.changed_attributes == {}
    assert not owner.parameters_dirty


def test_detached_observer_bindings_do_not_notify_original():
    original = full_process_run().steps["Acquire"]
    dsl.bind_attribute_observers(original)
    copied = original.model_copy(deep=True)
    dsl.bind_attribute_observers(copied)
    copied.parameters.acquisition.captured_at.unit = "s"
    assert copied.parameters_dirty
    assert not original.parameters_dirty
    assert original.parameters.acquisition.changed_attributes == {}
    unbound = original.model_copy(deep=True)
    unbound.parameters.acquisition.captured_at.unit = "ms"
    assert not original.parameters_dirty


def test_observation_state_is_not_serialized():
    owner = full_process_run().steps["Acquire"]
    before = owner.model_dump(mode="json")
    dsl.bind_attribute_observers(owner)
    owner.parameters.acquisition.captured_at.unit = None
    assert owner.parameters_dirty
    assert owner.model_dump(mode="json") == before


@pytest.mark.parametrize("domain", ["properties", "parameters"])
def test_conservative_comparison_preserves_unobserved_metadata_edits(domain):
    owner = (
        full_resource()
        if domain == "properties"
        else full_process_run().steps["Acquire"]
    )
    groups = getattr(owner, domain)
    group = next(iter(groups.values()))
    baseline = dsl.attribute_group_payload(group)
    dsl.bind_attribute_observers(owner)
    group["Captured At"].metadata_json["note"] = "corrected"
    assert not getattr(owner, f"{domain}_dirty")
    current = dsl.attribute_group_payload(group, baseline)
    assert current["Captured At"]["metadata_json"] == {"note": "corrected"}
    assert baseline["Captured At"]["metadata_json"] == {}


def test_builder_tracks_parameter_metadata_and_scalar_changes(client):
    client.create_namespace("observed-parameters")
    scoped = client.namespace("observed-parameters")
    with scoped.build_process_template("Transfer", "1.0") as template:
        template.add_step("Transfer").param_group("Transfer").add_attribute(
            "volume", "float", "uL", 10
        )
    with scoped.build_process_run("run", "desc", "Transfer", "1.0") as builder:
        pass
    run_id = builder.process_run.id
    with scoped.build_process_run(process_run_id=run_id) as builder:
        step = builder.process_run.steps["Transfer"]
        step.parameters.transfer.volume = 20
        assert step.parameters_dirty
        step.parameters.transfer.volume.metadata_json["note"] = "corrected"
        builder.set_model(builder.process_run)
        assert builder.changes().fields["steps"]["Transfer"]["parameters"]["Transfer"][
            "volume"
        ]["metadata_json"] == {"note": "corrected"}
    assert builder.process_run.steps[
        "Transfer"
    ].parameters.transfer.volume.metadata_json == {"note": "corrected"}
    with scoped.build_process_run(process_run_id=run_id) as builder:
        value = builder.process_run.steps["Transfer"].parameters.transfer.volume
        assert value.value == 20
        assert value.metadata_json == {"note": "corrected"}


def test_resource_builder_observes_property_edits_and_resets_after_save(client):
    client.create_namespace("observed-properties")
    scoped = client.namespace("observed-properties")
    with scoped.build_resource_template(
        name="Sample", type_names=["sample"]
    ) as template:
        template.prop_group("Content").add_attribute(
            "volume", "float", "uL", 10
        ).close_group()
    with scoped.build_resource("sample", "Sample") as builder:
        builder.save()
        builder.resource.properties["Content"].volume = 20
        assert builder.resource.properties_dirty
    assert not builder.resource.properties_dirty
    with scoped.build_resource(resource_id=builder.resource.id) as updated:
        updated.resource.properties.content.volume.metadata_json["note"] = "corrected"
    assert updated.resource.properties["Content"].volume.metadata_json == {
        "note": "corrected"
    }


def test_process_draft_edits_persist_without_set_model(client):
    client.create_namespace("draft-observed")
    scoped = client.namespace("draft-observed")
    with scoped.build_process_template("Transfers", "1") as template:
        template.add_step("Transfer").param_group("Transfer").add_attribute(
            "volume", "float", "uL", 10
        )
    with scoped.build_process_run("run", "", "Transfers", "1") as builder:
        pass
    run_id = builder.process_run.id
    with scoped.build_process_run(process_run_id=run_id) as builder:
        builder.process_run.steps["Transfer"].parameters.transfer.volume = 20
    with scoped.build_process_run(process_run_id=run_id) as builder:
        assert (
            builder.process_run.steps["Transfer"].parameters.transfer.volume.value == 20
        )
        builder.process_run.steps["Transfer"].parameters.transfer.volume = 10
        assert builder.changes().fields["steps"]
        builder.process_run.steps["Transfer"].parameters.transfer.volume = 20
        revision = builder.process_run.revision
    assert builder.process_run.revision == revision


def test_conservative_array_mutations_are_detected():
    from recap.schemas.step import ParameterSchema, StepSchema, StepTemplateSchema

    from .transport_factories import attribute_group, fields

    group_template = attribute_group()
    attribute = group_template.attribute_templates[0]
    attribute.value_type = "array"
    attribute.default_value = []
    group = ParameterSchema(
        **fields(template=group_template, values={"Captured At": []})
    )
    owner = StepSchema(
        **fields(
            name="Array",
            template=StepTemplateSchema(**fields(name="Array")),
            parameters={"Measurements": group},
        )
    )
    baseline = dsl.attribute_group_payload(group)
    dsl.bind_attribute_observers(owner)
    group.captured_at.value.append(1)
    assert not owner.parameters_dirty
    assert dsl.attribute_group_payload(group, baseline)["Captured At"]["value"] == [1]
    assert baseline["Captured At"]["value"] == []
