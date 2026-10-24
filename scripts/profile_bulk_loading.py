"""Attribute cold graph SQL without modifying the measured SQLite database."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from unittest.mock import patch
from uuid import UUID

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import UnboundExecutionError
from sqlalchemy.orm import Session, object_session, sessionmaker

from recap.adapter.entity_batch import EntityBatchCollector
from recap.adapter.local import LocalBackend
from recap.commands.service import CommandService
from recap.db.process import ProcessRun
from recap.db.resource import Resource
from recap.dsl.query import QuerySpec
from recap.schemas.process import ProcessRunSchema
from recap.schemas.resource import ResourceSchema


@dataclass
class SQLProfile:
    total_sql: int = 0
    sql_by_phase: Counter[str] = field(default_factory=Counter)
    lazy_relationships: Counter[str] = field(default_factory=Counter)
    planned_relationships: Counter[str] = field(default_factory=Counter)
    column_refreshes: int = 0
    loaded_types: Counter[str] = field(default_factory=Counter)
    callers: Counter[str] = field(default_factory=Counter)
    sql_by_kind: Counter[str] = field(default_factory=Counter)
    sql_kinds_by_phase: dict[str, Counter[str]] = field(default_factory=dict)
    lazy_by_phase: dict[str, Counter[str]] = field(default_factory=dict)
    callers_by_phase: dict[str, Counter[str]] = field(default_factory=dict)
    collected_records: Counter[str] = field(default_factory=Counter)
    phase: str = "preparation"

    @contextmanager
    def in_phase(self, phase):
        previous = self.phase
        self.phase = phase
        try:
            yield
        finally:
            self.phase = previous

    def report(self):
        return {
            "total_sql": self.total_sql,
            "sql_by_phase": dict(self.sql_by_phase),
            "lazy_relationships": dict(self.lazy_relationships),
            "planned_relationships": dict(self.planned_relationships),
            "column_refreshes": self.column_refreshes,
            "loaded_types": dict(self.loaded_types),
            "callers": dict(self.callers),
            "sql_by_kind": dict(self.sql_by_kind),
            "sql_kinds_by_phase": {
                phase: dict(counts) for phase, counts in self.sql_kinds_by_phase.items()
            },
            "lazy_by_phase": {
                phase: dict(counts) for phase, counts in self.lazy_by_phase.items()
            },
            "callers_by_phase": {
                phase: dict(counts) for phase, counts in self.callers_by_phase.items()
            },
            "collected_records": dict(self.collected_records),
        }


_PACKAGE_ROOT = str(Path(__file__).resolve().parents[1] / "recap") + "/"


def _recap_callers():
    """Capture code locations only; never SQL text, parameters, or frame locals."""
    locations = []
    frame = sys._getframe(1)
    try:
        while frame is not None:
            filename = frame.f_code.co_filename.replace("\\", "/")
            if filename.startswith(_PACKAGE_ROOT):
                relative = "recap/" + filename[len(_PACKAGE_ROOT) :]
                locations.append(f"{relative}:{frame.f_lineno}:{frame.f_code.co_name}")
            frame = frame.f_back
    finally:
        del frame
    return tuple(locations)


def _orm_attribution(state, token):
    category = "root"
    path = ""
    if state.is_relationship_load:
        category = "lazy" if state.lazy_loaded_from is not None else "planned"
        path = str(state.loader_strategy_path)
    elif state.is_column_load:
        category = "column"
    state.update_execution_options(
        _bulk_profile=(token, category, path, _recap_callers())
    )


def _count_statement(profile, token, context, statement):
    profile.total_sql += 1
    profile.sql_by_phase[profile.phase] += 1
    keyword = statement.lstrip().split(None, 1)[0].lower()
    kind = (
        "select"
        if keyword in {"select", "with"}
        else keyword
        if keyword in {"insert", "update", "delete"}
        else "other"
    )
    profile.sql_by_kind[kind] += 1
    profile.sql_kinds_by_phase.setdefault(profile.phase, Counter())[kind] += 1
    attribution = context.execution_options.get("_bulk_profile")
    if attribution is None or attribution[0] is not token:
        callers = _recap_callers()
        profile.callers.update(callers)
        profile.callers_by_phase.setdefault(profile.phase, Counter()).update(callers)
        return
    _, category, path, callers = attribution
    if category == "lazy":
        profile.lazy_relationships[path] += 1
        profile.lazy_by_phase.setdefault(profile.phase, Counter())[path] += 1
    elif category == "planned":
        profile.planned_relationships[path] += 1
    elif category == "column":
        profile.column_refreshes += 1
    profile.callers.update(callers)
    profile.callers_by_phase.setdefault(profile.phase, Counter()).update(callers)


def _owns_engine(session, engine, **bind_arguments):
    try:
        bind = session.get_bind(**bind_arguments)
    except UnboundExecutionError:
        return False
    return getattr(bind, "engine", bind) is engine


@contextmanager
def _command_phase_patches(profile, engine):
    original_snapshot = CommandService._graph_result
    original_flush = Session.flush

    def snapshot(row, schema):
        session = object_session(row)
        if session is not None and _owns_engine(session, engine):
            with profile.in_phase("snapshot_preparation"):
                return original_snapshot(row, schema)
        return original_snapshot(row, schema)

    def flush(session, *args, **kwargs):
        if _owns_engine(session, engine):
            with profile.in_phase("flush"):
                return original_flush(session, *args, **kwargs)
        return original_flush(session, *args, **kwargs)

    with (
        patch.object(CommandService, "_graph_result", staticmethod(snapshot)),
        patch.object(Session, "flush", flush),
    ):
        yield


@contextmanager
def capture_sql(engine: Engine, *, command_phases=False):
    """Track one engine and always restore collector/event instrumentation."""
    profile = SQLProfile(phase="mutation" if command_phases else "preparation")
    token = object()

    def orm_execute(state):
        if _owns_engine(state.session, engine, **state.bind_arguments):
            _orm_attribution(state, token)

    def before_cursor(conn, cursor, statement, parameters, context, executemany):
        _count_statement(profile, token, context, statement)

    def loaded(session, instance):
        if _owns_engine(session, engine, mapper=inspect(instance).mapper):
            profile.loaded_types[type(instance).__name__] += 1

    original_collect = EntityBatchCollector.collect

    def collect(collector):
        previous = profile.phase
        with profile.in_phase("collection"):
            batch = original_collect(collector)
        profile.collected_records[previous] += len(batch.entities)
        return batch

    listeners = [
        (engine, "before_cursor_execute", before_cursor),
        (Session, "do_orm_execute", orm_execute),
        (Session, "loaded_as_persistent", loaded),
    ]
    installed = []
    try:
        for target, name, listener in listeners:
            event.listen(target, name, listener)
            installed.append((target, name, listener))
        with ExitStack() as stack:
            stack.enter_context(patch.object(EntityBatchCollector, "collect", collect))
            if command_phases:
                stack.enter_context(_command_phase_patches(profile, engine))
            yield profile
    finally:
        for target, name, listener in reversed(installed):
            event.remove(target, name, listener)


def profile_read(
    factory, namespace_path, object_id, *, kind="resource", preloads=(), snapshot=False
):
    """Return a flat batch and attribution from a fresh session for each operation."""
    schema = ResourceSchema if kind == "resource" else ProcessRunSchema
    model = Resource if kind == "resource" else ProcessRun
    if kind not in {"resource", "process_run"}:
        raise ValueError("Unsupported profile kind")
    with capture_sql(factory.kw["bind"]) as profile:
        if snapshot:
            with factory() as session:
                row = session.get(model, object_id)
                if row is None:
                    raise ValueError("Profile root not found")
                if row.namespace.path != namespace_path:
                    raise ValueError("Profile root belongs to a different namespace")
                batch = CommandService._graph_result(row, schema)
        else:
            batch = LocalBackend(factory).query_batch(
                schema,
                QuerySpec(
                    filters={"id": object_id},
                    preloads=preloads,
                    load_mode="none" if preloads else "eager",
                    include_mutable=True,
                ),
                namespace_path=namespace_path,
            )
            if not batch.roots:
                raise ValueError("Profile root not found in namespace")
    return batch, profile


def profile_cases(factory, namespace_path, plate_id, well_id, run_id):
    reports = []
    for label, object_id, kind, preloads, snapshot in (
        ("plate eager", plate_id, "resource", (), False),
        ("well eager", well_id, "resource", (), False),
        ("plate children-only", plate_id, "resource", ("children",), False),
        ("run eager", run_id, "process_run", (), False),
        ("plate snapshot", plate_id, "resource", (), True),
        ("well snapshot", well_id, "resource", (), True),
        ("run snapshot", run_id, "process_run", (), True),
    ):
        start = perf_counter()
        batch, profile = profile_read(
            factory,
            namespace_path,
            object_id,
            kind=kind,
            preloads=preloads,
            snapshot=snapshot,
        )
        reports.append(
            {
                "operation": label,
                "seconds": perf_counter() - start,
                "roots": len(batch.roots),
                "records": len(batch.entities),
                "record_kinds": dict(Counter(record.kind for record in batch.entities)),
                **profile.report(),
            }
        )
    return reports


def readonly_engine(path):
    """SQLite itself rejects writes, including accidental future diagnostic writes."""
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError("Database path must be a file")
    return create_engine(f"sqlite+pysqlite:///{path.as_uri()}?mode=ro&uri=true")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--plate-id", required=True, type=UUID)
    parser.add_argument("--well-id", required=True, type=UUID)
    parser.add_argument("--run-id", required=True, type=UUID)
    args = parser.parse_args()
    engine = readonly_engine(args.db)
    try:
        reports = profile_cases(
            sessionmaker(bind=engine, autoflush=False),
            args.namespace,
            args.plate_id,
            args.well_id,
            args.run_id,
        )
        print(json.dumps(reports, indent=2, sort_keys=True))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
