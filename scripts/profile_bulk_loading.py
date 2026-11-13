"""Attribute cold graph SQL without modifying the measured SQLite database."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from time import perf_counter
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from recap.tests.helpers.sql_profile import (  # noqa: F401
    SQLProfile,
    capture_sql,
    profile_read,
)


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
