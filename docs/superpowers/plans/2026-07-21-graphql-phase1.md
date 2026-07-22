# GraphQL Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only GraphQL HTTP server to recap and a `GraphQLAdapter` + `RecapClient.from_url()` so clients transparently read via GraphQL while writing directly to SQLite.

**Architecture:** Split the existing `Backend` protocol into `ReadBackend` + `WriteBackend`. Implement `GraphQLAdapter(ReadBackend)` that POSTs to a Strawberry/FastAPI `/graphql` endpoint. `RecapClient.from_url()` fetches `/db_path` from the server, uses `GraphQLAdapter` for reads and `LocalBackend` for writes.

**Tech Stack:** Python 3.10+, SQLAlchemy 2.0, Pydantic v2, Strawberry-GraphQL, FastAPI, uvicorn, pydantic-settings, pyyaml, httpx

## Global Constraints

- Python ≥ 3.10
- Pydantic ≥ 2.0 (v2 API only — no v1 compat shims)
- SQLAlchemy ≥ 2.0
- All server deps (`strawberry-graphql[fastapi]`, `uvicorn`, `pydantic-settings`, `httpx`) go under `pyrecap[server]` optional extra in `pyproject.toml`
- `httpx` also added to core deps (used by `from_url()` which is not server-only)
- Tests: `pixi run -e dev test` (= `pytest -s -ra recap/tests`)
- Lint: `pixi run -e dev lint` (= `pre-commit run --all-files`)
- Follow ruff line-length 88, ruff-format enforced
- No mutations/writes via GraphQL in Phase 1
- Server default result limit: 1000 rows; max: 10,000
- `from_url()` requires shared filesystem between client and server (Phase 1 constraint — documented, not worked around)

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `recap/adapter/__init__.py` | Modify | Split `Backend` → `ReadBackend` + `WriteBackend` + `Backend` |
| `recap/adapter/local.py` | Modify | Annotate as implementing `Backend` (no logic change) |
| `recap/adapter/graphql.py` | Create | `GraphQLAdapter(ReadBackend)`, `QuerySpecTranslator` |
| `recap/client/base_client.py` | Modify | Add `from_url()`, accept `read_backend`/`write_backend` |
| `recap/server/__init__.py` | Create | Empty |
| `recap/server/app.py` | Create | `create_app(db_path)` FastAPI factory |
| `recap/server/config.py` | Create | `ServerConfig(BaseSettings)`, `from_yaml()` |
| `recap/server/strawberry_types.py` | Create | Strawberry types from Pydantic schemas |
| `recap/server/strawberry_schema.py` | Create | Root `Query` type + `build_schema(backend)` |
| `recap/server/resolvers.py` | Create | One resolver per root field → `LocalBackend` directly |
| `recap/server/__main__.py` | Create | CLI entrypoint (`python -m recap.server`) |
| `recap/exceptions.py` | Modify | Add `RecapConnectionError` |
| `pyproject.toml` | Modify | Add `server` optional extra, `httpx` core dep, `recap-server` script |
| `recap/tests/test_graphql_adapter.py` | Create | Unit tests for `GraphQLAdapter` + `QuerySpecTranslator` |
| `recap/tests/test_graphql_server.py` | Create | Integration tests for server endpoints |
| `recap/tests/test_from_url.py` | Create | Integration tests for `RecapClient.from_url()` |

---

## Task 1: Split `Backend` protocol into `ReadBackend` + `WriteBackend`

**Files:**
- Modify: `recap/adapter/__init__.py`

**Interfaces:**
- Produces:
  - `ReadBackend` protocol with all read/get/find/query methods
  - `WriteBackend` protocol with all create/update/assign/add methods
  - `Backend(ReadBackend, WriteBackend, Protocol)` combined protocol (backward compat)

- [ ] **Step 1: Write failing test**

Create `recap/tests/test_protocol_split.py`:

```python
from recap.adapter import Backend, ReadBackend, WriteBackend
from recap.adapter.local import LocalBackend
from pathlib import Path
import tempfile, os

def test_read_backend_is_protocol():
    from typing import Protocol
    assert issubclass(ReadBackend, Protocol) or hasattr(ReadBackend, '__protocol_attrs__')

def test_write_backend_is_protocol():
    from typing import Protocol
    assert issubclass(WriteBackend, Protocol) or hasattr(WriteBackend, '__protocol_attrs__')

def test_local_backend_satisfies_backend():
    # LocalBackend must still satisfy the combined Backend protocol
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "test.db")
        lb = LocalBackend(db_path)
        # runtime_checkable would be ideal but Protocol doesn't require it;
        # just verify the key methods exist on both protocols
        assert hasattr(lb, 'query')
        assert hasattr(lb, 'create_campaign')
        assert hasattr(lb, 'count')
        assert hasattr(lb, 'create_resource')
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pixi run -e dev pytest recap/tests/test_protocol_split.py -v
```

Expected: `ImportError` — `ReadBackend` and `WriteBackend` don't exist yet.

- [ ] **Step 3: Edit `recap/adapter/__init__.py`**

Replace the single `Backend(Protocol)` class with three classes. The full file content:

```python
from typing import Any, Literal, Protocol, overload, runtime_checkable
from uuid import UUID

from pydantic import BaseModel

from recap.dsl.query import QuerySpec, SchemaT
from recap.schemas.attribute import AttributeGroupRef, AttributeTemplateSchema
from recap.schemas.process import (
    CampaignSchema,
    ProcessRunSchema,
    ProcessTemplateRef,
    ProcessTemplateSchema,
)
from recap.schemas.resource import (
    ResourceRef,
    ResourceSchema,
    ResourceSlotSchema,
    ResourceTemplateRef,
    ResourceTemplateSchema,
    ResourceTypeSchema,
)
from recap.schemas.step import StepSchema, StepTemplateRef
from recap.utils.general import Direction


class UnitOfWork(Protocol):
    def commit(self, clear_session: bool = True) -> None: ...
    def rollback(self) -> None: ...
    def end_session(self) -> None: ...


@runtime_checkable
class ReadBackend(Protocol):
    """Read-only backend contract. Implemented by LocalBackend and GraphQLAdapter."""

    def query(self, schema: type[SchemaT], spec: QuerySpec) -> list[SchemaT]: ...
    def count(self, schema: type[SchemaT], spec: QuerySpec) -> int: ...

    @overload
    def get_process_template(
        self,
        name: str | None,
        version: str | None,
        expand: Literal[False],
        id: UUID | str | None = None,
    ) -> ProcessTemplateRef: ...

    @overload
    def get_process_template(
        self,
        name: str | None,
        version: str | None,
        expand: Literal[True],
        id: UUID | str | None = None,
    ) -> ProcessTemplateSchema: ...

    @overload
    def get_resource_template(
        self,
        name: str | None,
        version: str | None = None,
        id: UUID | str | None = None,
        parent: ResourceTemplateRef | ResourceTemplateSchema | None = None,
        expand: Literal[False] = False,
    ) -> ResourceTemplateRef: ...

    @overload
    def get_resource_template(
        self,
        name: str | None,
        version: str | None = None,
        id: UUID | str | None = None,
        parent: ResourceTemplateRef | ResourceTemplateSchema | None = None,
        expand: Literal[True] = False,
    ) -> ResourceTemplateSchema: ...

    def get_resource(
        self,
        name: str,
        template_name: str,
        template_version: str | None = "1.0",
        expand: bool = False,
    ) -> ResourceSchema: ...

    def find_resources_by_identity(
        self,
        name: str,
        parent_id: UUID | None,
        resource_template_id: UUID,
    ) -> list: ...

    def get_steps(self, process_run: ProcessRunSchema) -> list[StepSchema]: ...
    def get_params(self, step_schema: StepSchema) -> type[BaseModel]: ...


@runtime_checkable
class WriteBackend(Protocol):
    """Write-only backend contract. LocalBackend (Phase 1), RESTAdapter (Phase 2)."""

    def begin(self) -> UnitOfWork: ...

    def create_campaign(
        self,
        name: str,
        proposal: str,
        saf: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> CampaignSchema: ...

    def set_campaign(self, id: UUID) -> CampaignSchema: ...
    def update_campaign(self, campaign: CampaignSchema) -> CampaignSchema: ...

    def create_process_template(self, name: str, version: str) -> ProcessTemplateRef: ...

    def add_resource_slot(
        self,
        name: str,
        resource_type: str,
        direction: Direction,
        process_template_ref: ProcessTemplateRef,
        create_resource_type: bool = False,
        required: bool = True,
    ) -> ResourceSlotSchema: ...

    def add_step(self, name: str, process_template_ref: ProcessTemplateRef) -> StepTemplateRef: ...

    def bind_slot(
        self,
        role: str,
        slot_name: str,
        step_template_ref: StepTemplateRef,
        process_template_ref: ProcessTemplateRef,
    ) -> ResourceSlotSchema: ...

    def add_attr_group(
        self,
        group_name: str,
        template: ResourceTemplateRef | StepTemplateRef,
    ) -> AttributeGroupRef: ...

    def add_attribute(
        self,
        name: str,
        value_type: str,
        unit: str,
        default: Any,
        group: AttributeGroupRef,
    ) -> AttributeTemplateSchema: ...

    def remove_attribute(self, name: str) -> None: ...

    def add_resource_types(self, type_names: list[str]) -> list[ResourceTypeSchema]: ...

    def add_resource_template(
        self, name: str, type_names: list[ResourceTypeSchema], version: str = "1.0"
    ) -> ResourceTemplateRef: ...

    def add_child_resource_template(
        self,
        name: str,
        resource_types: list[ResourceTypeSchema],
        parent_resource_template: ResourceTemplateRef | ResourceTemplateSchema,
        version: str = "1.0",
    ) -> ResourceTemplateRef: ...

    @overload
    def create_resource(
        self,
        name: str,
        resource_template: ResourceTemplateRef | ResourceTemplateSchema,
        parent_resource: ResourceRef | ResourceSchema | None,
        expand: Literal[False],
    ) -> ResourceRef: ...

    @overload
    def create_resource(
        self,
        name: str,
        resource_template: ResourceTemplateRef | ResourceTemplateSchema,
        parent_resource: ResourceRef | ResourceSchema | None,
        expand: Literal[True],
    ) -> ResourceSchema: ...

    def add_child_resources(
        self,
        parent_resource: ResourceSchema | ResourceRef,
        child_resources: list[ResourceSchema | ResourceRef],
    ) -> None: ...

    def update_resource(self, resource: ResourceSchema) -> ResourceSchema: ...

    def create_process_run(
        self,
        name: str,
        description: str,
        process_template: ProcessTemplateRef | ProcessTemplateSchema,
        campaign: CampaignSchema,
    ) -> ProcessRunSchema: ...

    def assign_resource(
        self,
        resource_slot: ResourceSlotSchema,
        resource: ResourceRef | ResourceSchema,
        process_run: ProcessRunSchema,
    ) -> ProcessRunSchema: ...

    def check_resource_assignment(
        self,
        process_template: ProcessTemplateRef | ProcessTemplateSchema,
        process_run: ProcessRunSchema,
    ) -> None: ...

    def update_process_run(self, process_run: ProcessRunSchema) -> ProcessRunSchema: ...

    def add_child_step(
        self,
        process_run: ProcessRunSchema,
        child_step: StepSchema,
    ) -> StepSchema: ...

    def set_params(self, filled_params: type[BaseModel]) -> None: ...


class Backend(ReadBackend, WriteBackend, Protocol):
    """Combined read+write protocol. Implemented by LocalBackend."""
    pass
```

- [ ] **Step 4: Run tests**

```bash
pixi run -e dev pytest recap/tests/test_protocol_split.py -v
```

Expected: all 3 tests PASS.

- [ ] **Step 5: Run full test suite to check no regressions**

```bash
pixi run -e dev test
```

Expected: all existing tests PASS.

- [ ] **Step 6: Commit**

```bash
git add recap/adapter/__init__.py recap/tests/test_protocol_split.py
git commit -m "refactor: split Backend protocol into ReadBackend + WriteBackend"
```

---

## Task 2: Add `RecapConnectionError` and `httpx` dependency

**Files:**
- Modify: `recap/exceptions.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `RecapConnectionError(Exception)` importable from `recap.exceptions`

- [ ] **Step 1: Read existing exceptions**

```bash
cat recap/exceptions.py
```

- [ ] **Step 2: Write failing test**

Add to a new `recap/tests/test_exceptions.py`:

```python
def test_recap_connection_error_importable():
    from recap.exceptions import RecapConnectionError
    err = RecapConnectionError("http://localhost:8000", 404)
    assert "http://localhost:8000" in str(err)
    assert "404" in str(err)

def test_recap_connection_error_is_exception():
    from recap.exceptions import RecapConnectionError
    assert issubclass(RecapConnectionError, Exception)
```

- [ ] **Step 3: Run to verify failure**

```bash
pixi run -e dev pytest recap/tests/test_exceptions.py -v
```

Expected: `ImportError` — `RecapConnectionError` doesn't exist yet.

- [ ] **Step 4: Add `RecapConnectionError` to `recap/exceptions.py`**

Append to the existing file (after reading its current content):

```python
class RecapConnectionError(Exception):
    """Raised when RecapClient cannot connect to a recap server."""

    def __init__(self, url: str, status_code: int | None = None, message: str | None = None):
        self.url = url
        self.status_code = status_code
        detail = f" (HTTP {status_code})" if status_code else ""
        extra = f": {message}" if message else ""
        super().__init__(f"Cannot connect to recap server at {url}{detail}{extra}")
```

- [ ] **Step 5: Add `httpx` to core deps in `pyproject.toml`**

In the `[project]` `dependencies` list, add:

```toml
dependencies = [
    "sqlalchemy>=2.0",
    "pydantic>=2.0",
    "python-slugify",
    "mcp[cli]>=1.26.0,<2",
    "httpx>=0.27",
]
```

- [ ] **Step 6: Run tests**

```bash
pixi run -e dev pytest recap/tests/test_exceptions.py -v
```

Expected: both tests PASS.

- [ ] **Step 7: Commit**

```bash
git add recap/exceptions.py recap/tests/test_exceptions.py pyproject.toml
git commit -m "feat: add RecapConnectionError and httpx core dependency"
```

---

## Task 3: Add `pyrecap[server]` optional extra and server dependencies

**Files:**
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `pyrecap[server]` installable extra that pulls in `strawberry-graphql[fastapi]`, `uvicorn`, `pydantic-settings`, `pyyaml`
- Produces: `recap-server` console script entry point at `recap.server.__main__:main`

- [ ] **Step 1: Edit `pyproject.toml`**

Add under `[project]`:

```toml
[project.optional-dependencies]
server = [
    "strawberry-graphql[fastapi]>=0.220",
    "uvicorn[standard]>=0.30",
    "pydantic-settings>=2.0",
    "pyyaml>=6.0",
]
dev = ["pytest"]

[project.scripts]
recap-server = "recap.server.__main__:main"
```

- [ ] **Step 2: Add server feature to pixi for local dev**

In `pyproject.toml` pixi section, add a `server` feature and `dev` environment update:

```toml
[tool.pixi.feature.server.pypi-dependencies]
"strawberry-graphql" = {version = ">=0.220", extras = ["fastapi"]}
uvicorn = {version = ">=0.30", extras = ["standard"]}
pydantic-settings = ">=2.0"

[tool.pixi.environments]
default = { solve-group = "default" }
dev = { features = ["lint", "test", "server"], solve-group = "default" }
docs = { features = ["docs"], solve-group = "default" }
build = { features = ["build"], solve-group = "default" }
```

- [ ] **Step 3: Verify install resolves**

```bash
pixi install -e dev
```

Expected: resolves without conflicts.

- [ ] **Step 4: Verify strawberry importable**

```bash
pixi run -e dev python -c "import strawberry; print(strawberry.__version__)"
```

Expected: prints a version string.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml
git commit -m "build: add pyrecap[server] optional extra with strawberry/fastapi/uvicorn"
```

---

## Task 4: `ServerConfig` using `pydantic-settings`

**Files:**
- Create: `recap/server/__init__.py`
- Create: `recap/server/config.py`
- Create: `recap/tests/test_server_config.py`

**Interfaces:**
- Produces:
  - `ServerConfig(BaseSettings)` with fields: `db_path: Path`, `host: str = "127.0.0.1"`, `port: int = 8000`, `log_level: str = "info"`
  - `ServerConfig.from_yaml(path) -> ServerConfig` classmethod
  - Env var prefix: `RECAP_` (e.g. `RECAP_DB_PATH`, `RECAP_PORT`)

- [ ] **Step 1: Write failing tests**

Create `recap/tests/test_server_config.py`:

```python
import os
import tempfile
from pathlib import Path
import yaml
import pytest

def test_server_config_defaults():
    from recap.server.config import ServerConfig
    cfg = ServerConfig(db_path="/tmp/test.db")
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8000
    assert cfg.log_level == "info"
    assert cfg.db_path == Path("/tmp/test.db")

def test_server_config_from_yaml(tmp_path):
    from recap.server.config import ServerConfig
    config_file = tmp_path / "recap-server.yaml"
    config_file.write_text(yaml.dump({"server": {"db_path": str(tmp_path / "recap.db"), "port": 9000}}))
    cfg = ServerConfig.from_yaml(config_file)
    assert cfg.port == 9000
    assert cfg.db_path == tmp_path / "recap.db"

def test_server_config_from_yaml_missing_db_path(tmp_path):
    from recap.server.config import ServerConfig
    from pydantic import ValidationError
    config_file = tmp_path / "recap-server.yaml"
    config_file.write_text(yaml.dump({"server": {"port": 9000}}))
    with pytest.raises(ValidationError):
        ServerConfig.from_yaml(config_file)

def test_server_config_env_override(tmp_path, monkeypatch):
    from recap.server.config import ServerConfig
    monkeypatch.setenv("RECAP_PORT", "7777")
    monkeypatch.setenv("RECAP_DB_PATH", str(tmp_path / "env.db"))
    cfg = ServerConfig()
    assert cfg.port == 7777
```

- [ ] **Step 2: Run to verify failure**

```bash
pixi run -e dev pytest recap/tests/test_server_config.py -v
```

Expected: `ModuleNotFoundError` — `recap.server` doesn't exist.

- [ ] **Step 3: Create `recap/server/__init__.py`**

```python
"""recap GraphQL server package."""
```

- [ ] **Step 4: Create `recap/server/config.py`**

```python
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServerConfig(BaseSettings):
    """Configuration for the recap GraphQL server.

    Values can be set via:
    1. Environment variables prefixed with RECAP_ (e.g. RECAP_PORT=9000)
    2. YAML config file via ServerConfig.from_yaml()
    3. Direct instantiation kwargs
    """

    model_config = SettingsConfigDict(env_prefix="RECAP_")

    db_path: Path
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "info"

    @field_validator("db_path", mode="before")
    @classmethod
    def coerce_db_path(cls, v: object) -> Path:
        return Path(str(v))

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ServerConfig":
        """Load ServerConfig from a YAML file.

        YAML must have a top-level 'server:' key. db_path is required.
        CLI/env vars still override YAML values when set.
        """
        with open(path) as f:
            raw = yaml.safe_load(f)
        section = raw.get("server", {})
        return cls(**section)
```

- [ ] **Step 5: Run tests**

```bash
pixi run -e dev pytest recap/tests/test_server_config.py -v
```

Expected: all 4 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add recap/server/__init__.py recap/server/config.py recap/tests/test_server_config.py
git commit -m "feat: add ServerConfig with pydantic-settings and YAML support"
```

---

## Task 5: Strawberry types and schema

**Files:**
- Create: `recap/server/strawberry_types.py`
- Create: `recap/server/strawberry_schema.py`
- Create: `recap/tests/test_strawberry_types.py`

**Interfaces:**
- Consumes: all `recap/schemas/*.py` Pydantic models
- Produces:
  - Strawberry types: `CampaignType`, `ProcessRunType`, `ProcessTemplateType`, `ResourceType`, `ResourceTemplateType`, `StepType`
  - `build_schema(backend: LocalBackend) -> strawberry.Schema`

- [ ] **Step 1: Write failing test**

Create `recap/tests/test_strawberry_types.py`:

```python
def test_strawberry_types_importable():
    from recap.server.strawberry_types import (
        CampaignType,
        ProcessRunType,
        ProcessTemplateType,
        ResourceType,
        ResourceTemplateType,
        StepType,
    )
    assert CampaignType is not None

def test_build_schema_importable():
    from recap.server.strawberry_schema import build_schema
    assert callable(build_schema)
```

- [ ] **Step 2: Run to verify failure**

```bash
pixi run -e dev pytest recap/tests/test_strawberry_types.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Create `recap/server/strawberry_types.py`**

```python
"""Strawberry GraphQL types derived from recap Pydantic schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import strawberry
from strawberry.scalars import JSON


@strawberry.type
class AttributeValueType:
    id: strawberry.ID
    name: str
    slug: str
    value: JSON | None
    create_date: datetime
    modified_date: datetime


@strawberry.type
class AttributeGroupType:
    id: strawberry.ID
    name: str
    attributes: list[AttributeValueType]


@strawberry.type
class ResourceTemplateType:
    id: strawberry.ID
    name: str
    version: str
    create_date: datetime
    modified_date: datetime


@strawberry.type
class ResourceType:
    id: strawberry.ID
    name: str
    create_date: datetime
    modified_date: datetime
    template: ResourceTemplateType | None = None
    attribute_groups: list[AttributeGroupType] = strawberry.field(default_factory=list)


@strawberry.type
class ParameterType:
    name: str
    value: JSON | None


@strawberry.type
class StepType:
    id: strawberry.ID
    name: str
    status: str
    create_date: datetime
    modified_date: datetime
    parameters: list[ParameterType] = strawberry.field(default_factory=list)


@strawberry.type
class ProcessTemplateType:
    id: strawberry.ID
    name: str
    version: str
    create_date: datetime
    modified_date: datetime


@strawberry.type
class ResourceAssignmentType:
    resource: ResourceType
    slot_name: str
    direction: str


@strawberry.type
class ProcessRunType:
    id: strawberry.ID
    name: str
    description: str | None
    create_date: datetime
    modified_date: datetime
    process_template: ProcessTemplateType | None = None
    steps: list[StepType] = strawberry.field(default_factory=list)
    resources: list[ResourceAssignmentType] = strawberry.field(default_factory=list)


@strawberry.type
class CampaignType:
    id: strawberry.ID
    name: str
    proposal: str | None
    create_date: datetime
    modified_date: datetime
    process_runs: list[ProcessRunType] = strawberry.field(default_factory=list)
```

- [ ] **Step 4: Create `recap/server/strawberry_schema.py`**

```python
"""Root GraphQL Query type and schema builder."""
from __future__ import annotations

import strawberry
from strawberry.fastapi import GraphQLRouter

from recap.adapter.local import LocalBackend
from recap.server.resolvers import (
    resolve_campaigns,
    resolve_process_runs,
    resolve_process_templates,
    resolve_resource_templates,
    resolve_resources,
    resolve_resources_count,
    resolve_process_runs_count,
    resolve_campaigns_count,
)
from recap.server.strawberry_types import (
    CampaignType,
    ProcessRunType,
    ProcessTemplateType,
    ResourceTemplateType,
    ResourceType,
)


@strawberry.type
class Query:
    # List fields
    resources: list[ResourceType] = strawberry.field(resolver=resolve_resources)
    resource_templates: list[ResourceTemplateType] = strawberry.field(resolver=resolve_resource_templates)
    process_runs: list[ProcessRunType] = strawberry.field(resolver=resolve_process_runs)
    process_templates: list[ProcessTemplateType] = strawberry.field(resolver=resolve_process_templates)
    campaigns: list[CampaignType] = strawberry.field(resolver=resolve_campaigns)

    # Count fields
    resources_count: int = strawberry.field(resolver=resolve_resources_count)
    process_runs_count: int = strawberry.field(resolver=resolve_process_runs_count)
    campaigns_count: int = strawberry.field(resolver=resolve_campaigns_count)


def build_schema(backend: LocalBackend) -> strawberry.Schema:
    """Build a Strawberry schema with the given LocalBackend injected into resolver context."""

    async def get_context() -> dict:
        return {"backend": backend}

    return strawberry.Schema(query=Query)


def build_router(backend: LocalBackend) -> GraphQLRouter:
    """Build a GraphQLRouter (for mounting in FastAPI) with backend in context."""

    async def get_context() -> dict:
        return {"backend": backend}

    schema = strawberry.Schema(query=Query)
    return GraphQLRouter(schema, context_getter=get_context)
```

- [ ] **Step 5: Run tests**

```bash
pixi run -e dev pytest recap/tests/test_strawberry_types.py -v
```

Expected: both tests PASS.

- [ ] **Step 6: Commit**

```bash
git add recap/server/strawberry_types.py recap/server/strawberry_schema.py recap/tests/test_strawberry_types.py
git commit -m "feat: add Strawberry GraphQL types and schema builder"
```

---

## Task 6: Resolvers

**Files:**
- Create: `recap/server/resolvers.py`
- Create: `recap/tests/test_resolvers.py`

**Interfaces:**
- Consumes: `LocalBackend.query()`, `LocalBackend.count()`, `QuerySpec` from `recap.dsl.query`
- Produces: resolver functions consumed by `strawberry_schema.py` Query fields:
  - `resolve_resources(info, campaign_id, limit, offset) -> list[ResourceType]`
  - `resolve_resource_templates(info, limit, offset) -> list[ResourceTemplateType]`
  - `resolve_process_runs(info, campaign_id, limit, offset) -> list[ProcessRunType]`
  - `resolve_process_templates(info, limit, offset) -> list[ProcessTemplateType]`
  - `resolve_campaigns(info, limit, offset) -> list[CampaignType]`
  - `resolve_resources_count(info, campaign_id) -> int`
  - `resolve_process_runs_count(info, campaign_id) -> int`
  - `resolve_campaigns_count(info) -> int`

- [ ] **Step 1: Write failing tests**

Create `recap/tests/test_resolvers.py`:

```python
import tempfile, os
from uuid import UUID
import pytest
from unittest.mock import MagicMock

def make_mock_info(backend):
    info = MagicMock()
    info.context = {"backend": backend}
    return info

def test_resolve_resources_returns_list(tmp_path):
    from recap.adapter.local import LocalBackend
    from recap.server.resolvers import resolve_resources
    lb = LocalBackend(str(tmp_path / "test.db"))
    info = make_mock_info(lb)
    result = resolve_resources(info, campaign_id=None, limit=10, offset=0)
    assert isinstance(result, list)

def test_resolve_campaigns_returns_list(tmp_path):
    from recap.adapter.local import LocalBackend
    from recap.server.resolvers import resolve_campaigns
    lb = LocalBackend(str(tmp_path / "test.db"))
    info = make_mock_info(lb)
    result = resolve_campaigns(info, limit=10, offset=0)
    assert isinstance(result, list)

def test_resolve_resources_count(tmp_path):
    from recap.adapter.local import LocalBackend
    from recap.server.resolvers import resolve_resources_count
    lb = LocalBackend(str(tmp_path / "test.db"))
    info = make_mock_info(lb)
    count = resolve_resources_count(info, campaign_id=None)
    assert count == 0

def test_resolve_resources_enforces_max_limit(tmp_path):
    from recap.adapter.local import LocalBackend
    from recap.server.resolvers import resolve_resources
    import strawberry
    lb = LocalBackend(str(tmp_path / "test.db"))
    info = make_mock_info(lb)
    with pytest.raises(strawberry.exceptions.StrawberryGraphQLError):
        resolve_resources(info, campaign_id=None, limit=99999, offset=0)
```

- [ ] **Step 2: Run to verify failure**

```bash
pixi run -e dev pytest recap/tests/test_resolvers.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Create `recap/server/resolvers.py`**

```python
"""GraphQL resolver functions. Call LocalBackend directly — no RecapClient, no QueryDSL."""
from __future__ import annotations

from uuid import UUID

import strawberry

from recap.adapter.local import LocalBackend
from recap.dsl.query import QuerySpec
from recap.schemas.process import CampaignSchema, ProcessRunSchema, ProcessTemplateSchema
from recap.schemas.resource import ResourceSchema, ResourceTemplateSchema
from recap.server.strawberry_types import (
    CampaignType,
    ProcessRunType,
    ProcessTemplateType,
    ResourceAssignmentType,
    ResourceTemplateType,
    ResourceType,
    StepType,
)

_DEFAULT_LIMIT = 1000
_MAX_LIMIT = 10_000


def _check_limit(limit: int | None) -> int:
    effective = limit if limit is not None else _DEFAULT_LIMIT
    if effective > _MAX_LIMIT:
        raise strawberry.exceptions.StrawberryGraphQLError(
            f"Requested limit {effective} exceeds maximum allowed {_MAX_LIMIT}"
        )
    return effective


def _resource_schema_to_type(r: ResourceSchema) -> ResourceType:
    return ResourceType(
        id=strawberry.ID(str(r.id)),
        name=r.name,
        create_date=r.create_date,
        modified_date=r.modified_date,
    )


def _process_run_schema_to_type(pr: ProcessRunSchema) -> ProcessRunType:
    return ProcessRunType(
        id=strawberry.ID(str(pr.id)),
        name=pr.name,
        description=getattr(pr, "description", None),
        create_date=pr.create_date,
        modified_date=pr.modified_date,
    )


def _campaign_schema_to_type(c: CampaignSchema) -> CampaignType:
    return CampaignType(
        id=strawberry.ID(str(c.id)),
        name=c.name,
        proposal=getattr(c, "proposal", None),
        create_date=c.create_date,
        modified_date=c.modified_date,
    )


def _process_template_schema_to_type(pt: ProcessTemplateSchema) -> ProcessTemplateType:
    from recap.server.strawberry_types import ProcessTemplateType
    return ProcessTemplateType(
        id=strawberry.ID(str(pt.id)),
        name=pt.name,
        version=getattr(pt, "version", "1.0"),
        create_date=pt.create_date,
        modified_date=pt.modified_date,
    )


def _resource_template_schema_to_type(rt: ResourceTemplateSchema) -> ResourceTemplateType:
    return ResourceTemplateType(
        id=strawberry.ID(str(rt.id)),
        name=rt.name,
        version=getattr(rt, "version", "1.0"),
        create_date=rt.create_date,
        modified_date=rt.modified_date,
    )


def resolve_resources(
    info: strawberry.types.Info,
    campaign_id: strawberry.ID | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[ResourceType]:
    backend: LocalBackend = info.context["backend"]
    effective_limit = _check_limit(limit)
    spec = QuerySpec(
        campaign_id=UUID(str(campaign_id)) if campaign_id else None,
        limit=effective_limit,
        offset=offset,
    )
    results = backend.query(ResourceSchema, spec)
    return [_resource_schema_to_type(r) for r in results]


def resolve_resources_count(
    info: strawberry.types.Info,
    campaign_id: strawberry.ID | None = None,
) -> int:
    backend: LocalBackend = info.context["backend"]
    spec = QuerySpec(campaign_id=UUID(str(campaign_id)) if campaign_id else None)
    return backend.count(ResourceSchema, spec)


def resolve_resource_templates(
    info: strawberry.types.Info,
    limit: int | None = None,
    offset: int | None = None,
) -> list[ResourceTemplateType]:
    backend: LocalBackend = info.context["backend"]
    effective_limit = _check_limit(limit)
    spec = QuerySpec(limit=effective_limit, offset=offset)
    results = backend.query(ResourceTemplateSchema, spec)
    return [_resource_template_schema_to_type(r) for r in results]


def resolve_process_runs(
    info: strawberry.types.Info,
    campaign_id: strawberry.ID | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[ProcessRunType]:
    backend: LocalBackend = info.context["backend"]
    effective_limit = _check_limit(limit)
    spec = QuerySpec(
        campaign_id=UUID(str(campaign_id)) if campaign_id else None,
        limit=effective_limit,
        offset=offset,
    )
    results = backend.query(ProcessRunSchema, spec)
    return [_process_run_schema_to_type(pr) for pr in results]


def resolve_process_runs_count(
    info: strawberry.types.Info,
    campaign_id: strawberry.ID | None = None,
) -> int:
    backend: LocalBackend = info.context["backend"]
    spec = QuerySpec(campaign_id=UUID(str(campaign_id)) if campaign_id else None)
    return backend.count(ProcessRunSchema, spec)


def resolve_process_templates(
    info: strawberry.types.Info,
    limit: int | None = None,
    offset: int | None = None,
) -> list[ProcessTemplateType]:
    backend: LocalBackend = info.context["backend"]
    effective_limit = _check_limit(limit)
    spec = QuerySpec(limit=effective_limit, offset=offset)
    results = backend.query(ProcessTemplateSchema, spec)
    return [_process_template_schema_to_type(pt) for pt in results]


def resolve_campaigns(
    info: strawberry.types.Info,
    limit: int | None = None,
    offset: int | None = None,
) -> list[CampaignType]:
    backend: LocalBackend = info.context["backend"]
    effective_limit = _check_limit(limit)
    spec = QuerySpec(limit=effective_limit, offset=offset)
    results = backend.query(CampaignSchema, spec)
    return [_campaign_schema_to_type(c) for c in results]


def resolve_campaigns_count(info: strawberry.types.Info) -> int:
    backend: LocalBackend = info.context["backend"]
    spec = QuerySpec()
    return backend.count(CampaignSchema, spec)
```

- [ ] **Step 4: Run tests**

```bash
pixi run -e dev pytest recap/tests/test_resolvers.py -v
```

Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add recap/server/resolvers.py recap/tests/test_resolvers.py
git commit -m "feat: add GraphQL resolvers calling LocalBackend directly"
```

---

## Task 7: FastAPI app (`server/app.py`)

**Files:**
- Create: `recap/server/app.py`
- Create: `recap/tests/test_graphql_server.py`

**Interfaces:**
- Consumes: `build_router(backend)` from `strawberry_schema.py`, `LocalBackend`
- Produces: `create_app(db_path: str | Path) -> FastAPI` with `/graphql` and `/db_path` routes

- [ ] **Step 1: Write failing tests**

Create `recap/tests/test_graphql_server.py`:

```python
import tempfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient


def make_test_app(tmp_path):
    from recap.server.app import create_app
    return create_app(tmp_path / "test.db")


def test_db_path_endpoint(tmp_path):
    app = make_test_app(tmp_path)
    client = TestClient(app)
    resp = client.get("/db_path")
    assert resp.status_code == 200
    data = resp.json()
    assert "db_path" in data
    assert "test.db" in data["db_path"]


def test_graphql_endpoint_responds(tmp_path):
    app = make_test_app(tmp_path)
    client = TestClient(app)
    resp = client.post("/graphql", json={"query": "{ campaigns { id name } }"})
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    assert "campaigns" in body["data"]


def test_graphql_resources_empty(tmp_path):
    app = make_test_app(tmp_path)
    client = TestClient(app)
    resp = client.post("/graphql", json={"query": "{ resources { id name } }"})
    assert resp.status_code == 200
    assert resp.json()["data"]["resources"] == []


def test_graphql_count_fields(tmp_path):
    app = make_test_app(tmp_path)
    client = TestClient(app)
    resp = client.post("/graphql", json={"query": "{ resourcesCount campaignsCount }"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["resourcesCount"] == 0
    assert data["campaignsCount"] == 0
```

- [ ] **Step 2: Run to verify failure**

```bash
pixi run -e dev pytest recap/tests/test_graphql_server.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Create `recap/server/app.py`**

```python
"""FastAPI application factory for the recap GraphQL server."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from recap.adapter.local import LocalBackend
from recap.server.strawberry_schema import build_router


def create_app(db_path: str | Path) -> FastAPI:
    """Create the recap FastAPI application.

    Args:
        db_path: Path to the SQLite database file. Created if it doesn't exist.

    Returns:
        Configured FastAPI application with /graphql and /db_path endpoints.
    """
    db_path = Path(db_path)
    backend = LocalBackend(str(db_path))
    graphql_router = build_router(backend)

    app = FastAPI(
        title="recap GraphQL server",
        description="Read-only GraphQL API for recap experiment provenance data.",
        version="1.0.0",
    )

    app.include_router(graphql_router, prefix="/graphql")

    @app.get("/db_path", summary="Get database path")
    def get_db_path() -> dict[str, str]:
        """Return the path to the SQLite database file used by this server.

        Used by RecapClient.from_url() to wire direct SQLite writes in Phase 1.
        Requires shared filesystem between client and server.
        """
        return {"db_path": str(db_path.resolve())}

    return app
```

- [ ] **Step 4: Run tests**

```bash
pixi run -e dev pytest recap/tests/test_graphql_server.py -v
```

Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add recap/server/app.py recap/tests/test_graphql_server.py
git commit -m "feat: add FastAPI app factory with /graphql and /db_path endpoints"
```

---

## Task 8: CLI entrypoint (`server/__main__.py`)

**Files:**
- Create: `recap/server/__main__.py`

**Interfaces:**
- Produces: `main()` function — callable as `python -m recap.server` and as `recap-server` console script
- Consumes: `ServerConfig`, `ServerConfig.from_yaml()`, `create_app()`

- [ ] **Step 1: Create `recap/server/__main__.py`**

No TDD here — CLI entrypoint tested by running it. Create the file:

```python
"""CLI entrypoint for the recap GraphQL server.

Usage:
    python -m recap.server --db /path/to/recap.db
    python -m recap.server --config recap-server.yaml
    recap-server --db recap.db --port 8000
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="recap-server",
        description="recap GraphQL read API server",
    )
    parser.add_argument("--db", metavar="PATH", help="Path to SQLite database file")
    parser.add_argument("--config", metavar="PATH", help="Path to YAML config file")
    parser.add_argument("--host", default=None, help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default: 8000)")
    parser.add_argument("--log-level", default=None, dest="log_level", help="Log level (default: info)")
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("uvicorn not installed. Install with: pip install 'pyrecap[server]'", file=sys.stderr)
        sys.exit(1)

    from recap.server.config import ServerConfig

    if args.config:
        cfg = ServerConfig.from_yaml(args.config)
    elif args.db:
        cfg = ServerConfig(db_path=args.db)
    else:
        parser.error("Either --db or --config is required")

    # CLI flags override config file values
    if args.host is not None:
        cfg = cfg.model_copy(update={"host": args.host})
    if args.port is not None:
        cfg = cfg.model_copy(update={"port": args.port})
    if args.log_level is not None:
        cfg = cfg.model_copy(update={"log_level": args.log_level})

    from recap.server.app import create_app

    app = create_app(cfg.db_path)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level=cfg.log_level)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke test the CLI help**

```bash
pixi run -e dev python -m recap.server --help
```

Expected: prints usage with `--db`, `--config`, `--host`, `--port`, `--log-level` options.

- [ ] **Step 3: Commit**

```bash
git add recap/server/__main__.py
git commit -m "feat: add recap-server CLI entrypoint"
```

---

## Task 9: `GraphQLAdapter` and `QuerySpecTranslator`

**Files:**
- Create: `recap/adapter/graphql.py`
- Create: `recap/tests/test_graphql_adapter.py`

**Interfaces:**
- Consumes: `ReadBackend` protocol from `recap.adapter`, `QuerySpec` from `recap.dsl.query`
- Produces:
  - `GraphQLAdapter(graphql_url: str)` implementing `ReadBackend`
  - `GraphQLAdapter.query(schema, spec) -> list[SchemaT]`
  - `GraphQLAdapter.count(schema, spec) -> int`
  - `GraphQLAdapter.close()`, `__enter__`, `__exit__`
  - `QuerySpecTranslator(schema, spec)` — internal, not exported

- [ ] **Step 1: Write failing tests**

Create `recap/tests/test_graphql_adapter.py`:

```python
import pytest
from unittest.mock import patch, MagicMock
from recap.dsl.query import QuerySpec
from recap.schemas.resource import ResourceSchema
from recap.schemas.process import CampaignSchema


def test_graphql_adapter_importable():
    from recap.adapter.graphql import GraphQLAdapter
    assert GraphQLAdapter is not None


def test_graphql_adapter_implements_read_backend():
    from recap.adapter.graphql import GraphQLAdapter
    from recap.adapter import ReadBackend
    assert issubclass(GraphQLAdapter, ReadBackend) or hasattr(GraphQLAdapter, 'query')


def test_graphql_adapter_context_manager():
    from recap.adapter.graphql import GraphQLAdapter
    with GraphQLAdapter("http://localhost:9999/graphql") as adapter:
        assert adapter is not None


def test_query_spec_translator_resource_root_field():
    from recap.adapter.graphql import QuerySpecTranslator
    spec = QuerySpec()
    t = QuerySpecTranslator(ResourceSchema, spec)
    assert t.root_field_name() == "resources"


def test_query_spec_translator_campaign_root_field():
    from recap.adapter.graphql import QuerySpecTranslator
    spec = QuerySpec()
    t = QuerySpecTranslator(CampaignSchema, spec)
    assert t.root_field_name() == "campaigns"


def test_query_spec_translator_generates_query_string():
    from recap.adapter.graphql import QuerySpecTranslator
    spec = QuerySpec(limit=10, offset=0)
    t = QuerySpecTranslator(ResourceSchema, spec)
    q = t.to_graphql()
    assert "resources" in q
    assert "limit" in q
    assert "10" in q


def test_graphql_adapter_query_calls_post(tmp_path):
    from recap.adapter.graphql import GraphQLAdapter
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "data": {"resources": []}
    }
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.Client.post", return_value=mock_response):
        adapter = GraphQLAdapter("http://localhost:9999/graphql")
        spec = QuerySpec(limit=10)
        results = adapter.query(ResourceSchema, spec)
        assert results == []
        adapter.close()


def test_graphql_adapter_count_calls_post(tmp_path):
    from recap.adapter.graphql import GraphQLAdapter
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "data": {"resourcesCount": 42}
    }
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.Client.post", return_value=mock_response):
        adapter = GraphQLAdapter("http://localhost:9999/graphql")
        spec = QuerySpec()
        count = adapter.count(ResourceSchema, spec)
        assert count == 42
        adapter.close()
```

- [ ] **Step 2: Run to verify failure**

```bash
pixi run -e dev pytest recap/tests/test_graphql_adapter.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Create `recap/adapter/graphql.py`**

```python
"""GraphQL read-only adapter for RecapClient.

Implements ReadBackend by translating QuerySpec → GraphQL query strings
and posting to a recap GraphQL server endpoint.
"""
from __future__ import annotations

from typing import Literal
from uuid import UUID

import httpx

from recap.dsl.query import QuerySpec, SchemaT
from recap.schemas.process import (
    CampaignSchema,
    ProcessRunSchema,
    ProcessTemplateRef,
    ProcessTemplateSchema,
)
from recap.schemas.resource import (
    ResourceRef,
    ResourceSchema,
    ResourceTemplateRef,
    ResourceTemplateSchema,
)
from recap.schemas.step import StepSchema

# Maps Pydantic schema type → (list field name, count field name)
_SCHEMA_FIELD_MAP: dict[type, tuple[str, str]] = {
    ResourceSchema: ("resources", "resourcesCount"),
    ResourceTemplateSchema: ("resourceTemplates", "resourceTemplatesCount"),
    ProcessRunSchema: ("processRuns", "processRunsCount"),
    ProcessTemplateSchema: ("processTemplates", "processTemplatesCount"),
    CampaignSchema: ("campaigns", "campaignsCount"),
}

# Minimal field selections per schema type
_SCHEMA_FIELDS: dict[type, str] = {
    ResourceSchema: "id name createDate modifiedDate",
    ResourceTemplateSchema: "id name version createDate modifiedDate",
    ProcessRunSchema: "id name description createDate modifiedDate",
    ProcessTemplateSchema: "id name version createDate modifiedDate",
    CampaignSchema: "id name proposal createDate modifiedDate",
}


class QuerySpecTranslator:
    """Translates a QuerySpec + schema type into a GraphQL query string."""

    def __init__(self, schema: type, spec: QuerySpec):
        self._schema = schema
        self._spec = spec

    def root_field_name(self) -> str:
        return _SCHEMA_FIELD_MAP[self._schema][0]

    def count_field_name(self) -> str:
        return _SCHEMA_FIELD_MAP[self._schema][1]

    def _build_args(self) -> str:
        parts: list[str] = []
        spec = self._spec
        if spec.campaign_id is not None:
            parts.append(f'campaignId: "{spec.campaign_id}"')
        if spec.limit is not None:
            parts.append(f"limit: {spec.limit}")
        if spec.offset is not None:
            parts.append(f"offset: {spec.offset}")
        return f"({', '.join(parts)})" if parts else ""

    def _build_count_args(self) -> str:
        parts: list[str] = []
        spec = self._spec
        if spec.campaign_id is not None:
            parts.append(f'campaignId: "{spec.campaign_id}"')
        return f"({', '.join(parts)})" if parts else ""

    def to_graphql(self) -> str:
        field = self.root_field_name()
        args = self._build_args()
        fields = _SCHEMA_FIELDS.get(self._schema, "id name createDate modifiedDate")
        return f"{{ {field}{args} {{ {fields} }} }}"

    def to_graphql_count(self) -> str:
        field = self.count_field_name()
        args = self._build_count_args()
        return f"{{ {field}{args} }}"


class GraphQLAdapter:
    """ReadBackend implementation over HTTP GraphQL.

    Translates QuerySpec → GraphQL query string via QuerySpecTranslator,
    POSTs to the server, and deserializes JSON → Pydantic schemas.

    Phase 1 constraint: read-only. Write methods raise NotImplementedError.
    Use LocalBackend (via RecapClient.from_url()) for writes.
    """

    def __init__(self, graphql_url: str):
        self._url = graphql_url
        self._client = httpx.Client(timeout=30.0)

    def close(self) -> None:
        """Close the underlying HTTP client and release connections."""
        self._client.close()

    def __enter__(self) -> "GraphQLAdapter":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def query(self, schema: type[SchemaT], spec: QuerySpec) -> list[SchemaT]:
        translator = QuerySpecTranslator(schema, spec)
        gql = translator.to_graphql()
        response = self._client.post(self._url, json={"query": gql})
        response.raise_for_status()
        body = response.json()
        items = body["data"][translator.root_field_name()]
        return [schema.model_validate(item) for item in items]

    def count(self, schema: type[SchemaT], spec: QuerySpec) -> int:
        translator = QuerySpecTranslator(schema, spec)
        gql = translator.to_graphql_count()
        response = self._client.post(self._url, json={"query": gql})
        response.raise_for_status()
        return response.json()["data"][translator.count_field_name()]

    # ------------------------------------------------------------------ #
    # Read methods delegated to server (minimal implementations for now)
    # Full implementations to be added as needed in follow-up tasks.
    # ------------------------------------------------------------------ #

    def get_resource(self, name: str, template_name: str, template_version: str | None = "1.0", expand: bool = False) -> ResourceSchema:
        raise NotImplementedError("get_resource via GraphQL not yet implemented — use query()")

    def get_resource_template(self, name: str | None, version: str | None = None, id: UUID | str | None = None, parent=None, expand=False):
        raise NotImplementedError("get_resource_template via GraphQL not yet implemented — use query()")

    def get_process_template(self, name: str | None, version: str | None, expand=False, id: UUID | str | None = None):
        raise NotImplementedError("get_process_template via GraphQL not yet implemented — use query()")

    def find_resources_by_identity(self, name: str, parent_id: UUID | None, resource_template_id: UUID) -> list:
        raise NotImplementedError("find_resources_by_identity via GraphQL not yet implemented")

    def get_steps(self, process_run: ProcessRunSchema) -> list[StepSchema]:
        raise NotImplementedError("get_steps via GraphQL not yet implemented")

    def get_params(self, step_schema: StepSchema):
        raise NotImplementedError("get_params via GraphQL not yet implemented")
```

- [ ] **Step 4: Run tests**

```bash
pixi run -e dev pytest recap/tests/test_graphql_adapter.py -v
```

Expected: all 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add recap/adapter/graphql.py recap/tests/test_graphql_adapter.py
git commit -m "feat: add GraphQLAdapter and QuerySpecTranslator"
```

---

## Task 10: `RecapClient.from_url()`

**Files:**
- Modify: `recap/client/base_client.py`
- Create: `recap/tests/test_from_url.py`

**Interfaces:**
- Consumes: `GraphQLAdapter` from `recap.adapter.graphql`, `LocalBackend` from `recap.adapter.local`, `RecapConnectionError` from `recap.exceptions`
- Produces: `RecapClient.from_url(url: str) -> RecapClient` classmethod

- [ ] **Step 1: Read current `RecapClient.__init__` and `from_sqlite`**

```bash
cat recap/client/base_client.py
```

Note the constructor signature — you'll need to add `read_backend` / `write_backend` parameters (or detect the split internally).

- [ ] **Step 2: Write failing tests**

Create `recap/tests/test_from_url.py`:

```python
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


def make_server(tmp_path):
    from recap.server.app import create_app
    return TestClient(create_app(tmp_path / "recap.db"))


def test_from_url_returns_recap_client(tmp_path):
    from recap.client import RecapClient
    server = make_server(tmp_path)
    with patch("httpx.get") as mock_get:
        mock_get.return_value.json.return_value = {"db_path": str(tmp_path / "recap.db")}
        mock_get.return_value.raise_for_status = MagicMock()
        client = RecapClient.from_url("http://localhost:8000")
        assert isinstance(client, RecapClient)


def test_from_url_connection_error(tmp_path):
    from recap.client import RecapClient
    from recap.exceptions import RecapConnectionError
    import httpx
    with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
        with pytest.raises(RecapConnectionError):
            RecapClient.from_url("http://localhost:9999")


def test_from_url_bad_status(tmp_path):
    from recap.client import RecapClient
    from recap.exceptions import RecapConnectionError
    import httpx
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "404", request=MagicMock(), response=MagicMock(status_code=404)
    )
    with patch("httpx.get", return_value=mock_resp):
        with pytest.raises(RecapConnectionError):
            RecapClient.from_url("http://localhost:8000")
```

- [ ] **Step 3: Run to verify failure**

```bash
pixi run -e dev pytest recap/tests/test_from_url.py -v
```

Expected: `AttributeError` — `RecapClient` has no `from_url`.

- [ ] **Step 4: Add `from_url()` to `RecapClient`**

After reading `base_client.py`, add the classmethod. The key changes:

1. Add `from_url()` classmethod
2. Ensure `RecapClient.__init__` can accept separate `read_backend` and `write_backend` (use the existing `backend` for both if `from_sqlite`, split them if `from_url`)

```python
@classmethod
def from_url(cls, url: str) -> "RecapClient":
    """Connect to a recap GraphQL server.

    Fetches /db_path from the server to get the SQLite file path,
    then uses GraphQLAdapter for reads and LocalBackend for direct writes.

    Phase 1 constraint: requires shared filesystem between client and server.
    The server's db_path must be accessible from the client machine.
    This constraint is removed in Phase 2 when writes route through REST.

    Args:
        url: Base URL of the recap server, e.g. "http://localhost:8000"

    Raises:
        RecapConnectionError: If the server is unreachable or returns an error.
    """
    import httpx
    from recap.adapter.graphql import GraphQLAdapter
    from recap.adapter.local import LocalBackend
    from recap.exceptions import RecapConnectionError

    try:
        response = httpx.get(f"{url.rstrip('/')}/db_path")
        response.raise_for_status()
    except httpx.ConnectError as e:
        raise RecapConnectionError(url, message=str(e)) from e
    except httpx.HTTPStatusError as e:
        raise RecapConnectionError(url, status_code=e.response.status_code) from e

    db_path = response.json()["db_path"]
    read_backend = GraphQLAdapter(graphql_url=f"{url.rstrip('/')}/graphql")
    write_backend = LocalBackend(db_path)

    return cls._from_backends(read_backend=read_backend, write_backend=write_backend)
```

Also add `_from_backends()` classmethod that constructs the client with split backends (adjust to match the existing constructor pattern you see in the file).

- [ ] **Step 5: Run tests**

```bash
pixi run -e dev pytest recap/tests/test_from_url.py -v
```

Expected: all 3 tests PASS.

- [ ] **Step 6: Run full test suite**

```bash
pixi run -e dev test
```

Expected: all tests PASS, no regressions.

- [ ] **Step 7: Commit**

```bash
git add recap/client/base_client.py recap/tests/test_from_url.py
git commit -m "feat: add RecapClient.from_url() with GraphQLAdapter for reads"
```

---

## Task 11: Integration test — server + client end-to-end

**Files:**
- Create: `recap/tests/test_graphql_integration.py`

**Interfaces:**
- Consumes: `create_app()`, `RecapClient.from_sqlite()`, GraphQL queries

- [ ] **Step 1: Create integration test**

```python
"""End-to-end integration: write via LocalBackend, read via GraphQL server."""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock


def test_write_local_read_graphql(tmp_path):
    """Write a campaign via LocalBackend directly; read it back via GraphQL."""
    from recap.client import RecapClient
    from recap.server.app import create_app

    db_path = tmp_path / "recap.db"

    # Write directly via local client
    local_client = RecapClient.from_sqlite(str(db_path))
    campaign = local_client.create_campaign(name="Test Campaign", proposal="P-001")
    local_client.close()

    # Read via GraphQL server
    app = create_app(db_path)
    test_client = TestClient(app)

    resp = test_client.post("/graphql", json={"query": "{ campaigns { id name proposal } }"})
    assert resp.status_code == 200
    data = resp.json()["data"]["campaigns"]
    assert len(data) == 1
    assert data[0]["name"] == "Test Campaign"
    assert data[0]["proposal"] == "P-001"


def test_graphql_resources_after_write(tmp_path):
    """Write resources via LocalBackend; verify they appear in GraphQL."""
    from recap.client import RecapClient
    from recap.server.app import create_app

    db_path = tmp_path / "recap.db"
    local_client = RecapClient.from_sqlite(str(db_path))

    with local_client.build_resource_template(name="Sample", resource_type="sample") as tmpl:
        pass

    with local_client.build_resource(name="S-001", resource_template=tmpl) as res:
        pass

    local_client.close()

    app = create_app(db_path)
    test_client = TestClient(app)
    resp = test_client.post("/graphql", json={"query": "{ resources { id name } resourcesCount }"})
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["resourcesCount"] == 1
    assert body["resources"][0]["name"] == "S-001"


def test_graphql_limit_enforced(tmp_path):
    """Server enforces max limit of 10000."""
    from recap.server.app import create_app
    app = create_app(tmp_path / "recap.db")
    test_client = TestClient(app)
    resp = test_client.post("/graphql", json={"query": "{ resources(limit: 99999) { id } }"})
    assert resp.status_code == 200
    body = resp.json()
    assert "errors" in body
```

- [ ] **Step 2: Run integration tests**

```bash
pixi run -e dev pytest recap/tests/test_graphql_integration.py -v
```

Expected: all 3 tests PASS.

- [ ] **Step 3: Run full suite one final time**

```bash
pixi run -e dev test
```

Expected: all tests PASS.

- [ ] **Step 4: Commit**

```bash
git add recap/tests/test_graphql_integration.py
git commit -m "test: add GraphQL integration tests (write local, read via GraphQL)"
```

---

## Self-Review Checklist

- [x] Protocol split (ReadBackend/WriteBackend) — Task 1
- [x] RecapConnectionError — Task 2
- [x] pyrecap[server] extra + httpx core dep — Task 3
- [x] ServerConfig (pydantic-settings, YAML, env vars) — Task 4
- [x] Strawberry types from Pydantic schemas — Task 5
- [x] Resolvers calling LocalBackend directly, default/max limit — Task 6
- [x] FastAPI app with /graphql + /db_path — Task 7
- [x] CLI entrypoint (--db and --config modes) — Task 8
- [x] GraphQLAdapter + QuerySpecTranslator — Task 9
- [x] RecapClient.from_url() with error handling — Task 10
- [x] End-to-end integration tests — Task 11
- [x] Phase 1 constraint (shared filesystem) documented in from_url() docstring
- [x] Count fields exposed on server (resourcesCount, processRunsCount, campaignsCount)
- [x] No mutations in Phase 1
