# Plan 3 Task 6 Report

Status: complete

Implemented Resource create, update, copy commands and canonical REST routes:

- Resource command models with revision/idempotency payloads.
- Resource create and mutable update with duplicate, lifecycle, revision, audit, and rollback handling.
- Deep graph copy with fresh resource/property/value IDs, copied-from root, source activation, descendant validation, source-read/destination-write authorization, and transactional rollback.
- Canonical POST/PATCH/copy routes with ETag and If-Match handling.
- Resource builder copy-on-write for frozen resources.
- Command and REST regression tests.

Verification:

- `pixi run -e dev pytest -q recap/tests/test_resource_commands.py recap/tests/test_resource_copy.py recap/tests/test_rest_resources.py`: 10 passed.
- `pixi run -e dev pytest -q`: 512 passed, 1 skipped, 5 existing warnings.
- `pixi run -e dev ruff check ...`: passed.

Concerns:

- Full suite requires about 2 minutes 15 seconds.
- Existing suite emits five unrelated warnings.
