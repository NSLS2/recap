from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import BaseModel

from recap.dsl.query import BaseQuery
from recap.exporters.protocol import ExportContext, Exporter
from recap.exporters.registry import (
    ExporterRegistry,
    default_exporter_registry,
)
from recap.schemas.namespace import NamespaceContext


class Record(BaseModel):
    id: int
    name: str


class FakeBackend:
    def __init__(self, item: Record):
        self.item = item
        self.query_calls = 0

    def query(self, model, spec, *, namespace_path):
        self.query_calls += 1
        return [self.item]

    def count(self, model, spec, *, namespace_path):
        raise AssertionError("export must not use count query")


class RecordQuery(BaseQuery[Record]):
    model = Record


@dataclass
class RecordsExporter:
    calls: list[tuple[ExportContext, Path | StringIO | None]]

    def export(self, context: ExportContext, destination):
        self.calls.append((context, destination))
        return {"items": [item.model_dump() for item in context.items]}


@pytest.fixture
def query_and_backend():
    item = Record(id=1, name="canonical")
    backend = FakeBackend(item)
    query = RecordQuery(
        backend,
        context=NamespaceContext(id=uuid4(), path="test/export"),
    )
    return query, backend, item


def test_export_executes_once_and_passes_canonical_items_and_destination(
    query_and_backend, tmp_path
):
    query, backend, item = query_and_backend
    exporter = RecordsExporter([])
    name = f"records-{uuid4().hex}"
    default_exporter_registry.register(name, exporter)
    destination = tmp_path / "records.out"

    result = query.export(name, destination)

    assert result == {"items": [{"id": 1, "name": "canonical"}]}
    assert backend.query_calls == 1
    context, received_destination = exporter.calls[0]
    assert context.query is query
    assert context.items[0] is item
    assert received_destination is destination


def test_export_forwards_file_object_and_is_deterministic(query_and_backend):
    query, _, _ = query_and_backend
    exporter = RecordsExporter([])
    name = f"records-{uuid4().hex}"
    default_exporter_registry.register(name, exporter)
    destination = StringIO()

    first = query.export(name, destination)
    second = query.export(name, destination)

    assert first == second
    assert [call[1] for call in exporter.calls] == [destination, destination]


def test_export_rejects_unknown_format(query_and_backend):
    query, _, _ = query_and_backend

    with pytest.raises(KeyError, match="missing"):
        query.export("missing")


def test_registry_rejects_duplicate_registration():
    registry = ExporterRegistry()
    exporter = RecordsExporter([])
    registry.register("records", exporter)

    with pytest.raises(ValueError, match="records"):
        registry.register("records", exporter)


def test_export_propagates_exporter_exception(query_and_backend):
    query, backend, _ = query_and_backend

    class FailingExporter:
        def export(self, context, destination):
            raise RuntimeError("export failed")

    name = f"failing-{uuid4().hex}"
    default_exporter_registry.register(name, FailingExporter())

    with pytest.raises(RuntimeError, match="export failed"):
        query.export(name)
    assert backend.query_calls == 1


def test_export_rejects_scalar_count_results(query_and_backend):
    query, _, _ = query_and_backend
    query._backend.item = 1
    exporter = RecordsExporter([])
    name = f"records-{uuid4().hex}"
    default_exporter_registry.register(name, exporter)

    with pytest.raises(TypeError, match="BaseModel"):
        query.export(name)
    assert exporter.calls == []


def test_export_protocol_is_format_neutral():
    source = inspect.getsource(importlib.import_module(Exporter.__module__)).lower()
    assert all(term not in source for term in ("json-ld", "rdf", "semantic", "ro-crate"))
