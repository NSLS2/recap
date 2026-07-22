# GraphQL Phase 1 Design — recap

**Date:** 2026-07-21  
**Scope:** Phase 1 — GraphQL read API + hybrid client  
**Status:** Approved

---

## Background and Motivation

recap tracks experiment provenance as a graph: `Campaign → ProcessRun → Step → Resource → Attributes`. This relational/graph-shaped data is a natural fit for GraphQL, which lets callers specify exactly what fields and depth they need in a single request — avoiding REST's over-fetch or N+1 call patterns.

GraphQL's strength is **reads**. Writes involve transaction management, rollback, and context managers that are cleaner over direct DB access or (eventually) REST. This design therefore splits concerns:

- **Reads:** GraphQL
- **Writes/mutations:** direct `LocalBackend` (Phase 1), REST (Phase 2)

This split is transparent to `RecapClient` users — they call the same Python API regardless of which transport is used.

---

## Phased Roadmap

| Phase | Scope |
|---|---|
| **1 (this spec)** | GraphQL HTTP server (read-only). `GraphQLAdapter` for reads. `from_url()` fetches db path, writes directly to SQLite. Shared filesystem required. |
| **2** | REST API with authn/authz, transaction management. `RESTAdapter` for writes. Shared filesystem no longer required. |
| **3** | Python client write methods routed through `RESTAdapter`. `LocalBackend` becomes optional/offline mode. |

---

## Architecture

### New modules

```
recap/
├── adapter/
│   ├── __init__.py           (modified — split Backend → ReadBackend + WriteBackend)
│   ├── local.py              (modified — now explicitly implements ReadBackend + WriteBackend)
│   └── graphql.py            (NEW — implements ReadBackend, HTTP GraphQL client)
├── server/                   (NEW)
│   ├── __init__.py
│   ├── app.py                (FastAPI app factory, /graphql + /db_path endpoints)
│   ├── strawberry_types.py   (Strawberry types derived from Pydantic schemas)
│   ├── strawberry_schema.py  (root Query type, wires resolvers to fields)
│   ├── resolvers.py          (resolver functions → LocalBackend directly)
│   └── config.py             (YAML config loader + ServerConfig dataclass)
└── client/
    └── base_client.py        (modified — from_url(), routes reads/writes by adapter)
```

### Dependency graph

```
RecapClient
  ├── write_backend: WriteBackend  →  LocalBackend (Phase 1)
  └── read_backend:  ReadBackend   →  LocalBackend (from_sqlite)
                                   →  GraphQLAdapter (from_url)

GraphQLAdapter
  └── HTTP POST /graphql  →  FastAPI app
                              └── resolvers.py  →  LocalBackend (server-side)

RecapClient.from_url()
  └── HTTP GET /db_path   →  FastAPI app  →  returns {"db_path": "..."}
```

---

## Protocol Split

`adapter/__init__.py` splits the existing `Backend` protocol into three:

```python
class ReadBackend(Protocol):
    """Read-only backend contract. Implemented by LocalBackend and GraphQLAdapter."""

    def query(self, schema: type[SchemaT], spec: QuerySpec) -> list[SchemaT]: ...
    def count(self, schema: type[SchemaT], spec: QuerySpec) -> int: ...

    def get_resource(self, ...) -> ResourceSchema: ...
    def get_resource_template(self, ...) -> ResourceTemplateRef | ResourceTemplateSchema: ...
    def get_process_template(self, ...) -> ProcessTemplateRef | ProcessTemplateSchema: ...
    def find_resources_by_identity(self, ...) -> list: ...
    def get_steps(self, process_run: ProcessRunSchema) -> list[StepSchema]: ...
    def get_params(self, step_schema: StepSchema) -> type[BaseModel]: ...

class WriteBackend(Protocol):
    """Write-only backend contract. Implemented by LocalBackend (Phase 1), RESTAdapter (Phase 2)."""

    def create_campaign(self, ...) -> CampaignSchema: ...
    def set_campaign(self, id: UUID) -> CampaignSchema: ...
    def update_campaign(self, campaign: CampaignSchema) -> CampaignSchema: ...
    def create_process_template(self, ...) -> ProcessTemplateRef: ...
    def add_resource_slot(self, ...) -> ResourceSlotSchema: ...
    def add_step(self, ...) -> StepTemplateRef: ...
    def bind_slot(self, ...) -> ResourceSlotSchema: ...
    def add_attr_group(self, ...) -> AttributeGroupRef: ...
    def add_attribute(self, ...) -> AttributeTemplateSchema: ...
    def remove_attribute(self, ...) -> None: ...
    def add_resource_types(self, ...) -> list[ResourceTypeSchema]: ...
    def add_resource_template(self, ...) -> ResourceTemplateRef: ...
    def add_child_resource_template(self, ...) -> ResourceTemplateRef: ...
    def create_resource(self, ...) -> ResourceRef | ResourceSchema: ...
    def add_child_resources(self, ...) -> None: ...
    def update_resource(self, resource: ResourceSchema) -> ResourceSchema: ...
    def create_process_run(self, ...) -> ProcessRunSchema: ...
    def assign_resource(self, ...) -> ProcessRunSchema: ...
    def check_resource_assignment(self, ...) -> None: ...
    def update_process_run(self, process_run: ProcessRunSchema) -> ProcessRunSchema: ...
    def add_child_step(self, ...) -> StepSchema: ...
    def set_params(self, filled_params: type[BaseModel]) -> None: ...

class Backend(ReadBackend, WriteBackend, Protocol):
    """Combined protocol. LocalBackend implements this."""
    pass
```

`LocalBackend` continues to implement `Backend` — no external change. `GraphQLAdapter` implements `ReadBackend` only.

---

## Server Design

### `server/app.py` — FastAPI app factory

`build_schema(backend)` is imported from `strawberry_schema.py`. There is no circular import: `app.py` imports from `strawberry_schema.py`; `strawberry_schema.py` imports from `resolvers.py` and `strawberry_types.py` only — neither imports from `app.py`.

The WebSocket route is registered for future subscription support but is **not implemented in Phase 1**. Clients connecting via WebSocket will receive a `not implemented` error. This is intentional and documented.

```python
def create_app(db_path: str | Path) -> FastAPI:
    backend = LocalBackend(db_path)
    schema = build_schema(backend)       # strawberry_schema.build_schema
    graphql_app = GraphQL(schema)

    app = FastAPI(title="recap GraphQL server")
    app.add_route("/graphql", graphql_app)
    # WebSocket route reserved for Phase 1+ subscriptions — not active yet
    # app.add_websocket_route("/graphql", graphql_app)

    @app.get("/db_path")
    def get_db_path() -> dict:
        return {"db_path": str(db_path)}

    return app
```

### `server/config.py` — configuration

`ServerConfig` uses `pydantic-settings` `BaseSettings`, giving free validation, env var override, and dotenv support. YAML loading is handled by a custom `settings_customise_sources` that reads the `server:` key. Unknown keys are ignored. `db_path` is required — missing it raises Pydantic's `ValidationError`.

```python
from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

class ServerConfig(BaseSettings):
    db_path: Path
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "info"

    model_config = SettingsConfigDict(env_prefix="RECAP_")

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ServerConfig":
        with open(path) as f:
            raw = yaml.safe_load(f)
        return cls(**raw.get("server", {}))
```

Precedence (highest → lowest): CLI flags → env vars (`RECAP_DB_PATH`, `RECAP_HOST`, etc.) → YAML config file → field defaults.

`pydantic-settings` is added to the `server` optional extra — not core.

YAML format:

```yaml
server:
  db_path: /data/recap.db
  host: 0.0.0.0
  port: 8000
  log_level: info
```

### Server startup — two modes

**Quick local (CLI flag):**

```bash
python -m recap.server --db /path/to/recap.db --host 127.0.0.1 --port 8000
# pixi entry point:
recap-server --db recap.db
```

**Config-driven:**

```bash
recap-server --config recap-server.yaml
```

CLI flags override config file values. Both modes call `create_app(db_path)` and run via `uvicorn`.

### `server/strawberry_types.py` — GraphQL types

Strawberry's Pydantic bridge (`strawberry.experimental.pydantic`) generates GraphQL types from existing Pydantic schemas. No field duplication.

UUIDs are serialized as `strawberry.ID` (string). Datetimes are serialized as ISO 8601 strings using Strawberry's built-in `DateTime` scalar. JSON attribute values (`DefaultValue`) use a custom `JSON` scalar (Strawberry provides `strawberry.scalars.JSON`).

```python
@strawberry.experimental.pydantic.type(model=ResourceSchema)
class ResourceType:
    ...

@strawberry.experimental.pydantic.type(model=ProcessRunSchema)
class ProcessRunType:
    ...

# etc. for CampaignSchema, StepSchema, AttributeValueSchema,
# ResourceTemplateSchema, ProcessTemplateSchema
```

### `server/resolvers.py` — resolver functions

Resolvers receive a `LocalBackend` instance via Strawberry context. They construct `QuerySpec` directly from GraphQL arguments and call `backend.query()` — no `QueryDSL`, no `RecapClient`. All resolvers are synchronous (`def`, not `async def`); the `context_getter` is async but Strawberry handles sync/async resolvers transparently.

A server-side default `limit` of 1000 is enforced when `limit` is not specified by the caller, preventing unbounded result sets. Callers can request up to 10,000 rows; above that the server raises a `GraphQLError`.

```python
def resolve_resources(
    info: strawberry.types.Info,
    campaign_id: strawberry.ID | None = None,
    limit: int | None = None,
    offset: int | None = None,
    property_filters: list[PropertyFilterInput] | None = None,
) -> list[ResourceType]:
    backend: LocalBackend = info.context["backend"]
    spec = QuerySpec(
        campaign_id=UUID(campaign_id) if campaign_id else None,
        limit=limit,
        offset=offset,
        property_filters=[pf.to_query_spec() for pf in (property_filters or [])],
    )
    results = backend.query(ResourceSchema, spec)
    return [ResourceType.from_pydantic(r) for r in results]
```

One resolver per root query field: `resources`, `resource_templates`, `process_runs`, `process_templates`, `campaigns`.

### `server/strawberry_schema.py` — root Query type

```python
@strawberry.type
class Query:
    resources: list[ResourceType] = strawberry.field(resolver=resolve_resources)
    resource_templates: list[ResourceTemplateType] = strawberry.field(resolver=resolve_resource_templates)
    process_runs: list[ProcessRunType] = strawberry.field(resolver=resolve_process_runs)
    process_templates: list[ProcessTemplateType] = strawberry.field(resolver=resolve_process_templates)
    campaigns: list[CampaignType] = strawberry.field(resolver=resolve_campaigns)

def build_schema(backend: LocalBackend) -> strawberry.Schema:
    async def get_context() -> dict:
        return {"backend": backend}
    return strawberry.Schema(query=Query, context_getter=get_context)
```

---

## Client Design

### `RecapClient.from_url()`

```python
@classmethod
def from_url(cls, url: str) -> RecapClient:
    """
    Connect to a recap GraphQL server.

    Fetches the server's db_path and uses it for direct SQLite writes.
    Requires shared filesystem between client and server (Phase 1 constraint).
    Phase 2: writes will route through REST API instead.
    """
    response = httpx.get(f"{url}/db_path")
    response.raise_for_status()
    db_path = response.json()["db_path"]

    read_backend = GraphQLAdapter(graphql_url=f"{url}/graphql")
    write_backend = LocalBackend(db_path)

    return cls(read_backend=read_backend, write_backend=write_backend)
```

**Phase 1 constraint:** client and server must share a filesystem. The server's `db_path` must be accessible from the client machine. This constraint is removed in Phase 2 when writes route through REST.

### `adapter/graphql.py` — `GraphQLAdapter`

`GraphQLAdapter` implements `__enter__`/`__exit__` (context manager) and `close()` to ensure the `httpx.Client` connection pool is properly released. It can also be used without a context manager; callers are responsible for calling `close()` in that case. `RecapClient` calls `close()` in its own `__exit__`.

`from_url()` wraps the GET to `/db_path` in a `try/except` with a `RecapConnectionError` (new exception type) that includes the URL and HTTP status for clear diagnostics.

```python
class GraphQLAdapter:
    """
    ReadBackend implementation over HTTP GraphQL.
    Translates QuerySpec → GraphQL query string, deserializes response → Pydantic schemas.
    """

    def __init__(self, graphql_url: str):
        self._url = graphql_url
        self._client = httpx.Client(timeout=30.0)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GraphQLAdapter":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def query(self, schema: type[SchemaT], spec: QuerySpec) -> list[SchemaT]:
        translator = QuerySpecTranslator(schema, spec)
        gql_query = translator.to_graphql()
        response = self._client.post(self._url, json={"query": gql_query})
        response.raise_for_status()
        data = response.json()["data"]
        root_field = translator.root_field_name()
        return [schema.model_validate(item) for item in data[root_field]]

    def count(self, schema: type[SchemaT], spec: QuerySpec) -> int:
        translator = QuerySpecTranslator(schema, spec)
        gql_query = translator.to_graphql_count()
        response = self._client.post(self._url, json={"query": gql_query})
        response.raise_for_status()
        return response.json()["data"][translator.count_field_name()]
```

Count queries use dedicated root fields (`resourcesCount`, `processRunsCount`, etc.) exposed on the server `Query` type alongside the list fields.

### `QuerySpecTranslator` — `QuerySpec` → GraphQL string

Lives in `adapter/graphql.py`. Maps `QuerySpec` fields to GraphQL arguments and selection sets:

| `QuerySpec` field | GraphQL mapping |
|---|---|
| `campaign_id` | `campaignId: "uuid"` argument |
| `limit` | `limit: N` argument |
| `offset` | `offset: N` argument |
| `property_filters` | `propertyFilters: [...]` argument (input type) |
| `parameter_filters` | `parameterFilters: [...]` argument (input type) |
| `orderings` | `orderBy: [...]` argument |
| `load_mode = "none"` | minimal field selection (id, name, dates) |
| `load_mode = "full"` | all fields + nested relations |
| `preloads` / `include()` | additional fields appended to selection set |

Schema type → root field name mapping (e.g. `ResourceSchema` → `"resources"`, `CampaignSchema` → `"campaigns"`).

---

## GraphQL Input Types (server-side)

Server exposes input types mirroring `QuerySpec` fields so translation is mechanical:

```graphql
input PropertyFilterInput {
  name: String!
  group: String
  op: FilterOp!      # eq | gt | gte | lt | lte | between | in
  value: JSON!
  upper: JSON
  valueType: String
}

input ParameterFilterInput {
  name: String!
  group: String
  step: String
  op: FilterOp!
  value: JSON!
  upper: JSON
  valueType: String
}

enum FilterOp {
  EQ GT GTE LT LTE BETWEEN IN
}
```

---

## New Dependencies

All server dependencies are optional under `pyrecap[server]` extra:

| Package | Purpose | Extra |
|---|---|---|
| `strawberry-graphql[fastapi]` | GraphQL schema + FastAPI integration | `server` |
| `uvicorn` | ASGI server | `server` |
| `pydantic-settings` | Config management (BaseSettings, env vars, YAML) | `server` |
| `pyyaml` | YAML parsing (used by config.py) | `server` |
| `httpx` | HTTP client for `GraphQLAdapter` + `from_url()` | core (useful broadly) |

`pyproject.toml`:
```toml
[project.optional-dependencies]
server = ["strawberry-graphql[fastapi]", "uvicorn", "pyyaml"]

[project.scripts]
recap-server = "recap.server.__main__:main"
```

---

## What's Explicitly Out of Scope (Phase 1)

- Authentication / authorization
- GraphQL mutations (all writes remain via `LocalBackend` directly)
- REST API
- Subscriptions (websocket route registered but not implemented)
- Remote writes (no shared filesystem required until Phase 2)
- DataLoader / N+1 batching (can be added to server later without client changes)

---

## Future Phases (reference)

**Phase 2 — REST API:**
- FastAPI REST endpoints for all write operations
- authn/authz (token-based)
- Transaction management: context managers map to REST request lifecycle
- `RESTAdapter` implements `WriteBackend`
- `from_url()` no longer requires shared filesystem

**Phase 3 — Full remote client:**
- `RecapClient` write methods route through `RESTAdapter`
- `LocalBackend` becomes standalone/offline mode
- `adapter/rest.py` alongside `adapter/local.py` and `adapter/graphql.py`
