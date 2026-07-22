# AGENTS.md

RECAP — experiment provenance framework. SQLAlchemy 2.0 + Pydantic v2 + Alembic.

## Commands (tooling is run via pixi, not bare PATH)

- Tests: `pixi run -e dev test` (= `pytest -s -ra recap/tests`). Tests live in `recap/tests/`, not a top-level `tests/`.
- Single test: `pixi run -e dev pytest recap/tests/test_client.py::test_name`
- Lint/format: `pixi run -e dev lint` (= `pre-commit run --all-files`; runs `ruff --fix` then `ruff-format`)
- Docs: `pixi run -e docs docs-build` (mkdocs `--strict`)
- Build: `pixi run -e build build` (hatch)

## Package layout

- Distribution name is `pyrecap` (PyPI), but you **import `recap`**. Public entry point: `from recap.client import RecapClient`, then `RecapClient.from_sqlite([path])`.
- `recap/client/` — `RecapClient`, the only public API surface.
- `recap/adapter/` — `Backend` Protocol + `LocalBackend` (SQLAlchemy impl). The backend abstraction exists for a future REST backend; only SQLite/local works today. `http(s)` URLs raise `NotImplementedError`.
- `recap/dsl/` — builders + Query DSL (`ResourceBuilder`, `ProcessRunBuilder`, `QueryDSL`/`QuerySpec`). Builders are context managers that own the transaction; **DB writes happen only through builders**, never by mutating a returned Pydantic model.
- `recap/schemas/` — Pydantic v2 `*Schema`/`*Ref` types returned to callers.
- `recap/db/` — SQLAlchemy ORM models; Alembic migrations in `recap/db/migrations/`.

## Gotchas

- Version is VCS-driven via `hatch-vcs` and written to generated `recap/_version.py` — do not hand-edit it.
- `requirements.txt` / `requirements-dev.txt` are stale placeholders. Real dependencies live in `pyproject.toml` (`[project.dependencies]` + pixi features).
- Lint config conflict: `[tool.black]` sets line-length 115 but ruff uses 88 with `E501` ignored. **ruff-format (via pre-commit) is the enforced formatter**; black is configured but not wired into pre-commit. Follow ruff.
- `RecapClient.from_sqlite()` auto-applies Alembic migrations (`recap/utils/migrations.py`). The `sqlalchemy.url = sqlite:///recap.db` in `alembic.ini` is only for direct `alembic` CLI use.
- `RecapClient.set_campaign()` caches active campaign: re-setting same ID is a no-op unless `force=True` (then it re-queries DB).
- `mcp[cli]` is a declared dependency, but there is no MCP server implementation in the tree yet — don't assume one exists.
- `query_maker()` campaign scoping filters resources via process-run assignments; resources never assigned to any run are invisible unless you use `unscoped=True`.

## Query DSL conventions

- Each query has a `shape="schema"|"ref"` and `load="none"|"full"`. `include(...)` is valid **only** with `shape="schema", load="none"` (raises `ValueError` with `load="full"`).
- Known performance traps (N+1 on `load="full"` resource queries, `on_existing="silent"` triple round-trip) are documented in the README "Performance Guide" — read it before writing batch/loop code.

## References

`README.md` is the authoritative usage guide for the builder API, Query DSL, and Performance Guide. Prefer it over inferring behaviour from code.
