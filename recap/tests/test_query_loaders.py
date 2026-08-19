import pytest

from recap.adapter.query_loaders import (
    PRELOAD_STATEMENTS,
    resolve_loader_options,
)
from recap.schemas.process import ProcessRunSchema
from recap.schemas.resource import ResourceSchema


def test_duplicate_preloads_are_resolved_once_in_request_order(monkeypatch):
    calls = []

    def preload_options(schema, name):
        calls.append(name)
        return [name]

    monkeypatch.setattr("recap.adapter.query_loaders.preload_options", preload_options)

    options = resolve_loader_options(
        ProcessRunSchema, ["steps", "steps.parameters", "steps"], None
    )

    assert calls == ["steps", "steps.parameters"]
    assert options[-2:] == ["steps", "steps.parameters"]


def test_eager_mode_expands_stable_declared_paths(monkeypatch):
    calls = []

    def preload_options(schema, name):
        calls.append(name)
        return [name]

    monkeypatch.setattr("recap.adapter.query_loaders.preload_options", preload_options)

    resolve_loader_options(ResourceSchema, [], "eager")

    assert calls == ["template", "properties", "children"]


def test_unsupported_preload_is_validation_error():
    with pytest.raises(ValueError, match="Unsupported preload 'parameters'"):
        resolve_loader_options(ResourceSchema, ["parameters"], None)


def test_every_declared_preload_has_loader_statements():
    for (schema, relation), statements in PRELOAD_STATEMENTS.items():
        assert statements, (schema, relation)
