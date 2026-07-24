"""Compare local ORM hydration with JSON serialization and Pydantic validation."""

from __future__ import annotations

import argparse
import statistics
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from recap.client import RecapClient


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
    payload, dump_seconds = timed(
        lambda: [model.model_dump(mode="json") for model in models], repeats
    )
    _, validation_seconds = timed(
        lambda: [schema.model_validate(item) for item in payload], repeats
    )
    count = len(models)
    print(
        f"{label}: count={count} "
        f"local={local_seconds * 1000:.1f}ms "
        f"model_dump={dump_seconds * 1000:.1f}ms "
        f"model_validate={validation_seconds * 1000:.1f}ms "
        f"transport_overhead={(dump_seconds + validation_seconds) * 1000:.1f}ms "
        f"model_validate/local={validation_seconds / local_seconds:.2f}x"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()

    from recap.schemas.process import ProcessRunSchema
    from recap.schemas.resource import ResourceSchema

    with RecapClient.from_sqlite(args.db) as client:
        query = client.query_maker(unscoped=True)
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
