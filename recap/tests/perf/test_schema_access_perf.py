"""Performance regression tests for schema attribute access.

``LoadAwareMixin`` overrides ``__getattribute__`` to warn/raise when an
*unloaded* relation field is accessed. That guard runs on every attribute read
of ``ResourceSchema`` / ``ProcessRunSchema`` / ``StepSchema`` and friends, so it
must stay cheap.

Two regressions are guarded here, both asserted structurally (call counts, not
wall-clock, so the tests are machine-independent):

1. **Fast path** -- when nothing is unloaded, the guard must not touch the
   relation-field set or ``_handle_unloaded`` at all.
2. **Single guard** -- exactly one ``__getattribute__`` frame per access. The
   subclasses used to define their own ``__getattribute__`` on top of the
   mixin's, doubling the guard on every read.
"""

from datetime import datetime
from uuid import uuid4

import pytest

from recap.schemas.common import LoadAwareMixin
from recap.schemas.process import ProcessRunSchema
from recap.schemas.resource import ResourceSchema
from recap.schemas.step import StepSchema

_NS = uuid4()


def _loaded_resource() -> ResourceSchema:
    r = ResourceSchema.model_construct(
        id=uuid4(),
        create_date=datetime(2026, 1, 1),
        modified_date=datetime(2026, 1, 1),
        namespace_id=_NS,
        status="ACTIVE",
        revision=1,
        name="well",
        children={},
        properties={},
    )
    r.set_loaded_relations(
        {"template": True, "parent": True, "children": True, "properties": True}
    )
    return r


@pytest.mark.performance
def test_fully_loaded_access_never_calls_handle_unloaded():
    """Reading fields of a fully-loaded model must skip the guard body.

    ``_handle_unloaded`` must not be invoked at all -- including for relation
    fields -- when no relation is unloaded.
    """
    r = _loaded_resource()

    calls = 0
    original = LoadAwareMixin._handle_unloaded

    def counting(self, field_name, include_hint):  # noqa: ANN001
        nonlocal calls
        calls += 1
        return original(self, field_name, include_hint)

    fields = ("id", "name", "revision", "children", "template", "properties")
    LoadAwareMixin._handle_unloaded = counting
    try:
        for _ in range(1000):
            for field in fields:
                getattr(r, field)
    finally:
        LoadAwareMixin._handle_unloaded = original

    assert calls == 0, (
        f"fully-loaded access invoked _handle_unloaded {calls} times; "
        f"the fast path should skip it entirely"
    )


@pytest.mark.performance
def test_single_getattribute_guard_per_access():
    """Each attribute access must pass through exactly one guard frame.

    Guards against the double-guard regression where a subclass
    ``__getattribute__`` and the mixin's both ran per access.
    """
    r = _loaded_resource()

    calls = 0
    original = LoadAwareMixin.__getattribute__

    def counting(self, name):  # noqa: ANN001
        nonlocal calls
        calls += 1
        return original(self, name)

    LoadAwareMixin.__getattribute__ = counting
    try:
        accesses = 500
        for _ in range(accesses):
            getattr(r, "name")  # noqa: B009
    finally:
        LoadAwareMixin.__getattribute__ = original

    # Exactly one guard frame per access. (Two would mean the double-guard is
    # back; the guard body itself does no further attribute reads on the fast
    # path.)
    assert calls == accesses, (
        f"expected {accesses} guard frames for {accesses} accesses, got {calls}"
    )


@pytest.mark.performance
def test_subclasses_do_not_redefine_getattribute():
    """The relation guard lives only on the mixin (single source of truth).

    If a subclass re-adds ``__getattribute__`` the double-guard returns, so pin
    it structurally.
    """
    for cls in (ResourceSchema, ProcessRunSchema, StepSchema):
        assert "__getattribute__" not in cls.__dict__, (
            f"{cls.__name__} redefines __getattribute__; the guard should only "
            f"live on LoadAwareMixin to avoid double-guarding every access"
        )
