"""Compare local ORM hydration with JSON serialization and Pydantic validation."""

from __future__ import annotations

import argparse
import statistics
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from recap.adapter.schema_registry import SCHEMA_ENTITY_KEYS
from recap.adapter.transport import QueryResult, hydrate_result
from recap.client import RecapClient
from recap.client.identity import IdentityMap
from recap.lifecycle import LifecycleStatus


def timed(call: Callable[[], Any], repeats: int) -> tuple[Any, float]:
    samples: list[float] = []
    result = None
    for _ in range(repeats):
        start = time.perf_counter()
        result = call()
        samples.append(time.perf_counter() - start)
    return result, statistics.median(samples)


def benchmark(
    label: str,
    query_factory: Callable[[], Any],
    schema: type[BaseModel],
    repeats: int,
) -> None:
    models, local_seconds = timed(lambda: query_factory().all(), repeats)
    _, schema_seconds = timed(
        lambda: [schema.model_validate(model.model_dump(mode="python")) for model in models],
        repeats,
    )
    payload, dump_seconds = timed(
        lambda: [model.model_dump(mode="json") for model in models], repeats
    )
    result = QueryResult(
        entity=SCHEMA_ENTITY_KEYS[schema], projection="full", items=payload
    )
    transport_models, validation_seconds = timed(
        lambda: hydrate_result(schema, result), repeats
    )
    _, materialization_seconds = timed(
        lambda: [model.build_property_model() for model in transport_models]
        if hasattr(schema, "model_fields") and "properties" in schema.model_fields
        else list(transport_models),
        repeats,
    )
    _, identity_seconds = timed(
        lambda: _merge_canonical(transport_models), repeats
    )
    count = len(models)
    print(
        f"{label}: count={count} "
        f"local={local_seconds * 1000:.1f}ms "
        f"database_query={local_seconds * 1000:.1f}ms "
        f"schema_construction={schema_seconds * 1000:.1f}ms "
        f"model_dump={dump_seconds * 1000:.1f}ms "
        f"json_transport_hydration={validation_seconds * 1000:.1f}ms "
        f"dynamic_model_materialization={materialization_seconds * 1000:.1f}ms "
        f"canonical_identity_merge={identity_seconds * 1000:.1f}ms "
        f"model_validate={validation_seconds * 1000:.1f}ms "
        f"transport_overhead={(dump_seconds + validation_seconds) * 1000:.1f}ms "
        f"model_validate/local={validation_seconds / local_seconds:.2f}x"
    )


def _merge_canonical(models: list[BaseModel]) -> list[BaseModel]:
    identity = IdentityMap()
    return [identity.intern(model) for model in models]


def _benchmark_client(client: RecapClient) -> RecapClient:
    """Create one small graph when benchmark database has no queryable namespace."""
    namespaces = client.list_namespaces()
    for path in namespaces:
        scoped = client.namespace(path)
        context = scoped._resolve_namespace_context()
        scoped._namespace_context = context
        if (
            context.status is LifecycleStatus.ACTIVE
            and scoped.query_maker().resources().count() > 0
            and scoped.query_maker().process_runs().count() > 0
        ):
            return scoped

    benchmark_path = f"benchmark/{uuid4().hex}"
    client.create_namespace(benchmark_path)
    scoped = client.namespace(benchmark_path)
    scoped._namespace_context = scoped._resolve_namespace_context()
    if scoped.query_maker().resources().count() == 0:
        with scoped.build_resource_template(
            name="BenchmarkResource", type_names=["sample"]
        ):
            pass
        resource = scoped.create_resource("sample-0", "BenchmarkResource")
        scoped.build_resource(resource_id=resource.id).activate()
    process_name = f"BenchmarkProcess-{uuid4().hex}"
    with scoped.build_process_template(process_name, "1.0") as template:
        template.add_step("Collect")
        template.activate()
    with scoped.build_process_run(
        "run-0", "benchmark", process_name, "1.0"
    ) as run:
        run.finalize()
    return scoped


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()

    from recap.schemas.process import ProcessRunSchema
    from recap.schemas.resource import ResourceSchema

    with RecapClient.from_sqlite(args.db) as client:
        query = _benchmark_client(client).query_maker()
        benchmark(
            "resources load=none",
            lambda: query.resources(load="none").limit(args.limit),
            ResourceSchema,
            args.repeats,
        )
        benchmark(
            "resources include=properties",
            lambda: query.resources(load="none")
            .include("properties")
            .limit(args.limit),
            ResourceSchema,
            args.repeats,
        )
        benchmark(
            "resources load=eager",
            lambda: query.resources(load="eager").limit(args.limit),
            ResourceSchema,
            args.repeats,
        )
        benchmark(
            "process_runs load=none",
            lambda: query.process_runs(load="none").limit(args.limit),
            ProcessRunSchema,
            args.repeats,
        )
        benchmark(
            "process_runs load=eager",
            lambda: query.process_runs(load="eager").limit(args.limit),
            ProcessRunSchema,
            args.repeats,
        )


if __name__ == "__main__":
    main()
