"""Draft-copy cost excludes referenced resource graphs."""

from collections import Counter

import pytest
from pydantic import BaseModel

from recap.dsl import drafts
from recap.schemas.resource import ResourceSchema
from recap.tests.helpers.transport_factories import full_process_run, full_resource


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
