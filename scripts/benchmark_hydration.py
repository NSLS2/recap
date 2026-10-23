"""Measure database collection, flat JSON transport, and shared materialization."""

from __future__ import annotations

import argparse
import statistics
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from recap.adapter.local import LocalBackend
from recap.adapter.schema_registry import SCHEMA_ENTITY_KEYS
from recap.adapter.transport import (
    FlatQueryResult,
    decode_query_batch,
    encode_query_batch,
)
from recap.client import RecapClient
from recap.client.identity import IdentityMap
from recap.client.materialization import BatchMaterializer
from recap.dsl.query import QuerySpec
from recap.exceptions import RecapNotFoundError
from recap.schemas.process import ProcessRunSchema
from recap.schemas.resource import ResourceSchema
from recap.utils.general import Direction


def timed(call: Callable[[], Any], repeats: int) -> tuple[Any, float]:
    samples = []
    result = None
    for _ in range(repeats):
        start = time.perf_counter()
        result = call()
        samples.append(time.perf_counter() - start)
    return result, statistics.median(samples)


def benchmark(
    label: str,
    backend: LocalBackend,
    schema: type[BaseModel],
    spec: QuerySpec,
    namespace_path: str,
    repeats: int,
) -> None:
    batch, collection_seconds = timed(
        lambda: backend.query_batch(schema, spec, namespace_path=namespace_path),
        repeats,
    )
    payload, encoding_seconds = timed(
        lambda: encode_query_batch(
            batch, entity=SCHEMA_ENTITY_KEYS[schema], projection="full"
        ).model_dump_json(),
        repeats,
    )
    decoded, decoding_seconds = timed(
        lambda: decode_query_batch(
            schema, FlatQueryResult.model_validate_json(payload)
        ),
        repeats,
    )
    _, cold_seconds = timed(
        lambda: BatchMaterializer(IdentityMap()).materialize(schema, decoded), repeats
    )
    materializer = BatchMaterializer(IdentityMap())
    materializer.materialize(schema, decoded)
    _, cached_seconds = timed(
        lambda: materializer.materialize(schema, decoded), repeats
    )
    print(
        f"{label}: roots={len(batch.roots)} records={len(batch.entities)} adjacency={len(batch.adjacency)} "
        f"database_collection={collection_seconds * 1000:.1f}ms "
        f"json_encoding={encoding_seconds * 1000:.1f}ms "
        f"json_decoding={decoding_seconds * 1000:.1f}ms "
        f"cold_materialization={cold_seconds * 1000:.1f}ms "
        f"cached_materialization={cached_seconds * 1000:.1f}ms"
    )


def _benchmark_client(client: RecapClient) -> RecapClient:
    """Reuse a queryable namespace, otherwise seed a small local workflow."""
    pending = list(client.list_namespaces())
    while pending:
        path = pending.pop()
        scoped = client.namespace(path)
        if (
            scoped.query_maker().resources().count()
            and scoped.query_maker().process_runs().count()
        ):
            return scoped
        pending.extend(f"{path}/{name}" for name in scoped.list_namespaces())
        scoped.close()

    try:
        parent = client.namespace("benchmark")
    except RecapNotFoundError:
        client.create_namespace("benchmark")
    else:
        parent.close()
    path = f"benchmark/{uuid4().hex}"
    client.create_namespace(path)
    scoped = client.namespace(path)
    with scoped.build_resource_template(
        name="BenchmarkResource", type_names=["sample"]
    ) as template:
        template.add_properties(
            {
                "Measurements": [
                    {"name": "Dose", "type": "int", "default": None},
                    {"name": "Readings", "type": "array", "default": [1, 1]},
                ]
            }
        )
        template.finalize()
    with scoped.build_resource("sample-0", "BenchmarkResource") as builder:
        builder.finalize()
    resource = builder.resource
    with scoped.build_process_template("BenchmarkProcess", "1.0") as template:
        template.add_resource_slot("sample", "sample", Direction.input)
        template.add_step("Collect").add_parameters(
            {"Exposure": [{"name": "Dwell", "type": "int", "default": 1}]}
        ).bind_slot("input", "sample").close_step()
        template.finalize()
    with scoped.build_process_run(
        "run-0", "benchmark", "BenchmarkProcess", "1.0"
    ) as run:
        run.assign_resource("sample", resource)
        run.finalize()
    return scoped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.limit < 1 or args.repeats < 1:
        parser.error("--limit and --repeats must be positive")
    with RecapClient.from_sqlite(args.db) as client:
        scoped = _benchmark_client(client)
        try:
            backend = scoped.connection_state.backend.reader
            scenarios = [
                (
                    "resources load=none",
                    ResourceSchema,
                    QuerySpec(limit=args.limit, load_mode="none"),
                ),
                (
                    "resources include=properties",
                    ResourceSchema,
                    QuerySpec(limit=args.limit, preloads=("properties",)),
                ),
                (
                    "resources load=eager",
                    ResourceSchema,
                    QuerySpec(limit=args.limit, load_mode="eager"),
                ),
                (
                    "process_runs load=none",
                    ProcessRunSchema,
                    QuerySpec(limit=args.limit, load_mode="none"),
                ),
                (
                    "process_runs load=eager",
                    ProcessRunSchema,
                    QuerySpec(limit=args.limit, load_mode="eager"),
                ),
            ]
            for label, schema, spec in scenarios:
                benchmark(
                    label, backend, schema, spec, scoped.namespace_path, args.repeats
                )
        finally:
            scoped.close()


if __name__ == "__main__":
    main()
