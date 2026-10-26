from collections import Counter

import pytest
from pydantic import BaseModel

from recap.dsl import drafts
from recap.schemas.resource import ResourceSchema
from recap.utils import dsl

from .transport_factories import full_process_run, full_resource


def test_draft_copy_preserves_owned_graph_aliases():
    resource = full_resource()
    child = resource.children["child"]
    child.parent = resource
    draft = drafts.draft_copy(resource)
    assert draft is not resource
    assert draft.children["child"] is not child
    assert draft.children["child"].parent is draft
    assert dsl.read_only_unwrap(draft.template) is resource.template
    assert dsl.read_only_unwrap(draft.parent) is resource.parent
    with pytest.raises(TypeError, match="read-only"):
        draft.template.name = "unsaved"
    draft.properties.measurements.captured_at.unit = "s"
    assert resource.properties.measurements.captured_at.unit is None


def test_draft_copy_detaches_observers_and_mutable_metadata():
    resource = full_resource()
    resource.properties.measurements.captured_at.metadata_json["source"] = "detector"
    dsl.bind_attribute_observers(resource)
    draft = drafts.draft_copy(resource)
    assert draft.properties.measurements.captured_at._observer.notify is None
    dsl.bind_attribute_observers(draft)
    draft.properties.measurements.captured_at.unit = "s"
    draft.properties.measurements.captured_at.metadata_json["source"] = ["changed"]
    assert draft.properties_dirty
    assert not resource.properties_dirty
    assert (
        resource.properties.measurements.captured_at.metadata_json["source"]
        == "detector"
    )


def test_run_copy_owns_steps_and_assignments_but_not_references():
    run = full_process_run()
    draft = drafts.draft_copy(run)
    assert draft.steps["Acquire"] is not run.steps["Acquire"]
    assert draft.assigned_resources["input"] is not run.assigned_resources["input"]
    resource = run.assigned_resources["input"].resource
    assert dsl.read_only_unwrap(draft.assigned_resources["input"].resource) is resource
    assert dsl.read_only_unwrap(draft.steps["Acquire"].resources["input"]) is resource
    with pytest.raises(TypeError, match="read-only"):
        draft.steps["Acquire"].resources["input"].name = "unsaved"
    draft.steps["Acquire"].parameters.acquisition.captured_at.unit = "s"
    assert run.steps["Acquire"].parameters.acquisition.captured_at.unit is None
    assert (
        drafts.draft_copy(draft).steps["Acquire"].template
        is draft.steps["Acquire"].template
    )


def test_owned_walk_handles_recursive_mutable_metadata():
    resource = full_resource()
    recursive = {}
    recursive["self"] = recursive
    resource.properties.measurements.captured_at.metadata_json["recursive"] = recursive
    draft = drafts.draft_copy(resource)
    copied = draft.properties.measurements.captured_at.metadata_json["recursive"]
    assert copied is not recursive
    assert copied["self"] is copied


@pytest.mark.performance
def test_well_and_run_copy_cost_excludes_referenced_plate(monkeypatch):
    original = BaseModel.__deepcopy__
    copied = Counter()

    def count(self, memo=None):
        copied[type(self)] += 1
        return original(self, memo)

    monkeypatch.setattr(BaseModel, "__deepcopy__", count)

    def measure(size, copy_plate=False, copy_run=False):
        plate = full_resource()
        plate.children = {}
        for i in range(size):
            well = full_resource()
            well.children = {}
            well.parent = plate
            plate.children[str(i)] = well
        run = full_process_run()
        run.assigned_resources["input"].resource = plate
        run.steps["Acquire"].resources["input"] = plate
        copied.clear()
        target = run if copy_run else plate if copy_plate else plate.children["0"]
        drafts.draft_copy(target)
        return copied.copy()

    assert measure(12) == measure(96)
    assert measure(12, copy_run=True) == measure(96, copy_run=True)
    assert measure(96, copy_plate=True)[ResourceSchema] == 97
