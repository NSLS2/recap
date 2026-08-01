# Plan 3 Task 3 Report

## Status

Complete. Commit message: `feat: expose namespace write commands`.

## Implementation

- Added `CommandService.create_namespace` and `update_namespace` with one owning
  mutation UoW.
- Added parent/root and target namespace authorization.
- Added canonical path validation, lifecycle transitions, optimistic revisions,
  idempotent replay, atomic success audit/result persistence, rollback, and
  durable failure audit.
- Added authenticated namespace `PUT` and `PATCH` routes, strict request models,
  `Idempotency-Key` and `If-Match` parsing, `ETag` responses, and safe command
  error mapping.
- Added command and REST coverage for success, replay, conflict, rollback,
  authorization, metadata, status, revision, audit, and required headers.

## TDD Evidence

- RED: `pixi run --frozen -e dev pytest -q recap/tests/test_namespace_commands.py recap/tests/test_rest_namespaces.py`
  failed during collection with `ModuleNotFoundError: recap.commands.service`.
- GREEN: same command passed: `14 passed in 3.57s`.

## Verification

- `pixi run --frozen -e dev pytest -q recap/tests`: `491 passed, 1 skipped`.
- `pixi run --frozen -e dev lint`: all hooks passed.

## Concerns

- Full suite retains five pre-existing warnings (one reuse warning and four DSL
  deprecation warnings).
- Runtime subagent depth prevented independent reviewer dispatch; local
  requirements and diff review found no open issue.
