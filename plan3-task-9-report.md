# Plan 3 Task 9 Report

Status: complete

Commit: `feat: add namespace REST client`

Implemented:

- Authenticated `RESTAdapter` with redacted credentials, canonical command URLs, idempotency headers, `If-Match`, ETag/request-ID capture, typed HTTP/connection errors, and timeout/connect mapping.
- Frozen `NamespaceClient` with query, builder, copy, and explicit creation delegation.
- Namespace-aware `RecapClient.namespace()` and query binding by path.
- Focused adapter and namespace tests.

Verification:

- `pixi run --frozen -e dev pytest -q recap/tests/test_rest_adapter.py recap/tests/test_namespace_client.py`: 8 passed
- `pixi run --frozen -e dev pytest -q`: 518 passed, 1 skipped
- `pixi run --frozen -e dev lint`: passed

Concerns:

- Remote read/write cutover and server resource/copy routes remain Task 10 scope.
- Existing full-suite warnings remain: idempotent template reuse and deprecated query predicate/order APIs.

## Fix

Fixed `NamespaceClient` delegation against actual `RecapClient` contracts. Builder wrappers now use explicit typed parameters and namespace paths; existing-ID builder calls avoid conflicting positional arguments. Copy now preserves source UUID, converts changes to `ResourceCopyOptions`, and passes explicit destination namespace path. Typed fakes cover AMX/FMX isolation and copy routing.

Verification:

- `pixi run --frozen -e dev pytest -q recap/tests/test_rest_adapter.py recap/tests/test_namespace_client.py recap/tests/test_client.py recap/tests/test_from_url.py`: 25 passed
- `pixi run --frozen -e dev pytest -q recap/tests/test_graphql_client_parity.py`: 47 passed
- `pixi run --frozen -e dev pre-commit run --files recap/adapter/rest.py recap/client/base_client.py recap/client/namespace_client.py recap/tests/test_namespace_client.py recap/tests/test_rest_adapter.py`: ruff passed; ruff format passed
- `pixi run --frozen -e dev lint`: ruff passed; repository-wide format hook touched two unrelated pre-existing files, so changes were removed; focused pre-commit lint above passed
