# REST API Branch Architecture

## Scope

This document describes changes made on branch `rest-api` after it diverged from
`main`.

- Base: `0b9e85f8d640d0e414f89161529e874509cf925b`
- Branch tip: `8e33371` (`Merge rest API implementation`)
- Branch range: `0b9e85f8..8e33371`

Behavior inherited from `main` is intentionally omitted. Code references point
to the branch-tip implementation.

## Architectural Summary

The branch changes RECAP from a locally scoped, Campaign-oriented persistence
model into a namespace-scoped service with authenticated reads and writes.

The resulting split is:

- **REST query endpoints** handle remote reads and counts.
- **REST command endpoints** handle remote create, update, and copy commands.
- **CommandService** owns authorization, transaction boundaries, revisions,
  idempotency, and mutation outcomes.
- **Namespace policy** determines what an authenticated actor may see or change.
- **Builders** retain the existing fluent API but accumulate drafts and submit
  one aggregate command at save time.
- **Query models** are canonical full models; relationship loading is controlled
  independently with `load="none"`, targeted includes, or `load="eager"`.

The main request flows are:

```text
Read:
  API key -> RequestActor -> QueryRequest -> NamespacePolicy
           -> authorized REST query -> LocalBackend query/count

Write:
  API key -> REST route -> CommandContext -> CommandService
          -> authorization -> idempotency -> one transaction
  -> revision/audit -> REST response with ETag
```

Query RPC uses ordinary JSON envelopes. `POST /api/v1/query` accepts an entity,
projection, namespace path, and serialized `QuerySpec`; it returns matching
serialized models. `POST /api/v1/query/count` returns a count envelope. Local
and remote clients use the same QueryDSL semantics and authorization ordering.

## 1. Namespace Domain

### Namespace replaces Campaign ownership

The first phase introduces hierarchical namespaces and moves provenance
aggregates under explicit namespace ownership. Namespace paths are canonical,
have parent/ancestor relationships, and carry lifecycle status and revisions.

Relevant locations:

- `recap/lifecycle.py:1-21` defines `LifecycleStatus` and allowed transitions.
- `recap/utils/namespace.py:1-38` canonicalizes paths and calculates parents,
  ancestors, and ancestry relationships.
- `recap/db/namespace.py:19-92` defines the namespace ORM model and repository.
- `recap/schemas/namespace.py:1-30` defines public namespace schemas and
  namespace query context.
- `recap/db/migrations/versions/71c5ce51c034_add_namespaces.py` backfills
  namespaces from Campaign data.
- `recap/db/migrations/versions/8f3c2a1b7d90_remove_campaigns.py` removes the
  Campaign schema after backfill validation.

The ownership model becomes explicit:

- Process templates: `(namespace_id, name, version)`.
- Resource templates: `(namespace_id, name, version)` for roots.
- Process runs: `(namespace_id, name)`.
- Top-level resources: UUID identity; names are not globally unique.

The ownership and identity changes are implemented in:

- `recap/db/process.py`
- `recap/db/resource.py`
- `recap/schemas/process.py`
- `recap/schemas/resource.py`
- `recap/adapter/local.py`

### Namespace query context

Namespace context travels beside serialized query state rather than inside
`QuerySpec`. This preserves query portability while making visibility explicit
at each backend boundary.

- `recap/dsl/query.py` carries namespace context on query objects.
- `recap/adapter/transport.py` carries `namespace_path` beside `QuerySpec`.
- `recap/adapter/local.py` applies namespace visibility before user filters,
  ordering, pagination, and counts.
- `recap/adapter/rest.py` serializes the namespace context for remote reads.
- `recap/server/rest.py` receives explicit namespace paths for REST query
  operations.

Process runs require exact namespace context. Templates and resources can be
visible through ancestor namespaces subject to authorization. Archived entities
are excluded unless explicitly requested.

### Lifecycle, freezing, and copy-on-write

The branch adds aggregate lifecycle and immutability rules:

- `MUTABLE -> ACTIVE -> ARCHIVED` is forward-only.
- Templates and resources freeze after first stable reference.
- Nested aggregate mutations are checked through the owning root.
- Frozen resources are copied into another namespace instead of edited in
  place.

Implementation locations:

- `recap/db/process.py` and `recap/db/resource.py` store lifecycle and revision
  state.
- `recap/adapter/local.py` enforces lifecycle operations and deep-copy behavior.
- `recap/db/step.py` and `recap/db/attribute.py` participate in aggregate freeze
  validation.
- `recap/dsl/resource_builder.py` exposes copy-on-write behavior.
- `recap/tests/test_resource_freeze.py` and
  `recap/tests/test_resource_copy.py` cover freeze and copy semantics.

Copy creates fresh IDs for the resource graph, preserves source identity through
`copied_from_id` on the root, applies requested property changes, activates the
source when required, and commits source activation plus destination insertion
atomically.

## 2. Authentication

Authentication produces an immutable request actor. The actor contains a stable
actor ID, provider identities, credential scopes, optional namespace
restrictions, and a credential fingerprint.

- `recap/authentication/models.py:1-34` defines `ProviderIdentity`, `ActorKind`,
  and frozen `RequestActor` models.
- `recap/authentication/protocols.py:1-15` defines authentication protocols.
- `recap/authentication/api_key.py:13-35` validates API keys and produces the
  request actor.
- `recap/server/security.py:22-36` parses the `Authorization: Apikey ...`
  header and maps authentication failures to server responses.
- `recap/server/config.py` validates authentication mode and credential config.

The branch defines separate scopes for namespace, template, resource, and
process-run read/write operations in `recap/authorization/scopes.py`. Write
scope does not implicitly grant read scope.

Secrets are kept out of representations, errors, and audit records. The
client-side redaction implementation is in `recap/adapter/rest.py:37-50`.

## 3. Authorization

Authorization is snapshot-based in multi-user mode. A source configuration maps
provider identities to groups, groups to roles, and roles to scopes at namespace
paths. Compilation happens outside request handling.

- `recap/authorization/source.py` defines source configuration models.
- `recap/authorization/compiler.py` compiles YAML authorization input into a
  validated SQLite snapshot.
- `recap/authorization/snapshot.py` loads immutable snapshot generations and
  fails closed for missing, corrupt, or stale snapshots.
- `recap/authorization/policy.py:32-205` evaluates effective permissions and
  target relationships.
- `recap/authorization/query.py` provides authorized query representations.

`SnapshotNamespacePolicy` evaluates:

1. Canonical namespace path.
2. Actor namespace restrictions.
3. Actor identity matches.
4. Ancestor grants.
5. Intersection with credential scopes.
6. Target relationship rules for the requested entity and operation.

Relationship rules differ by aggregate:

- Process-run access requires target and context namespaces to match.
- Template/resource reads may use ancestor visibility.
- Template/resource writes target the context namespace.
- Namespace creation authorizes against the parent namespace.
- Resource copy requires source resource read plus destination resource write.

`UnrestrictedNamespacePolicy` provides the single-user local/test policy through
the same interface.

## 4. Request Context, Errors, and Audit

Request state is immutable and assembled once per request.

- `recap/server/rest.py` authenticates each query request, acquires one
  authorization snapshot generation, and injects policy-aware querying.

The server also adds stable request IDs and sanitized error handling:

- `recap/server/errors.py` defines request error mapping and request-ID access.
- `recap/server/error_handlers.py` maps command and authorization failures to
  safe HTTP errors.
- `recap/server/audit.py:15-57` defines immutable `AuditRecord` and `AuditSink`.
- `recap/db/audit.py:14-56` persists mutation audit records.
- `recap/commands/audit.py:9-22` emits durable failure records after rollback.

Audit records capture request, actor, mutation, outcome, and safe reason data.
They do not contain credentials, property values, or process parameter values.

## 5. Command Architecture

The command layer is the write-side application boundary. REST routes and local
builder paths both converge on command models and `CommandService` methods.

### Command models and request scope

- `recap/commands/models.py:1-89` defines strict immutable command DTOs.
- `recap/commands/models.py` defines `CommandContext` with actor, request ID,
  policy, audit sink, authorization generation, and idempotency key.
- `recap/commands/service.py:110-181` dispatches command DTOs to aggregate
  handlers.
- `recap/db/engine.py` and `recap/server/dependencies.py` provide a
  request-scoped session/backend.

Each command handler owns one database transaction. The normal sequence is:

```text
authorize -> calculate fingerprint -> claim/replay idempotency
-> load and validate aggregate -> mutate -> increment revision
-> persist success audit/result -> commit
```

Failure rolls back the mutation and emits failure audit through a separate short
transaction.

### Idempotency and optimistic concurrency

- `recap/commands/idempotency.py:1-26` creates deterministic fingerprints from
  method, route, namespace, source ID, and body.
- `recap/db/idempotency.py` stores unique actor/key claims and replay responses.
- `recap/commands/service.py:193-270` shows the create-resource transaction,
  including authorization, fingerprinting, idempotency replay, mutation, and
  audit.
- Revision checks use `If-Match` values and return entity revisions as ETags.

Replays re-check current authorization before returning the saved result. A
changed route, namespace, source ID, or body conflicts with the existing key.

## 6. REST API

The REST server is a thin HTTP adapter over `CommandService`.

- `recap/server/rest.py:39-64` defines the `/api/v1` router and builds
  `CommandContext`.
- `recap/server/rest.py:72-79` parses `If-Match` revisions.
- `recap/server/rest.py:81-319` defines namespace, template, resource, copy, and
  process-run mutation routes.
- `recap/server/rest_models.py` defines strict request payloads.

Mutation route families:

```text
PUT   /api/v1/namespaces/{namespace_path}
PATCH /api/v1/namespaces/{namespace_id}

POST  /api/v1/process-templates/{namespace_path}
PATCH /api/v1/process-templates/{template_id}

POST  /api/v1/resource-templates/{namespace_path}
PATCH /api/v1/resource-templates/{template_id}

POST  /api/v1/resources/{namespace_path}
PATCH /api/v1/resources/{resource_id}
POST  /api/v1/resources/{source_resource_id}/copies

POST  /api/v1/process-runs/{namespace_path}
PATCH /api/v1/process-runs/{process_run_id}
```

Creates and copies require `Idempotency-Key`. Updates require both
`Idempotency-Key` and `If-Match`. Successful responses include `ETag` with the
new revision.

Copy requests send destination namespace in the body rather than the URL:

```json
{
  "destination_namespace": "beamline/amx",
  "name": "AMXRobot",
  "changes": {"properties": {"tool": "new-gripper"}}
}
```

## 7. REST Client and Remote Cutover

`RESTAdapter` is the authenticated query and command transport:

- `recap/adapter/rest.py:53-106` owns HTTP, authentication headers, idempotency
  headers, ETags, request IDs, typed HTTP errors, and connection errors.
- `recap/adapter/rest.py:108-184` maps query, namespace, create, update, and
  copy operations to canonical routes.
- `recap/adapter/rest.py:186-279` maps command DTOs to REST calls and validates
  response schemas.

`RecapClient.namespace()` returns a client view bound to one canonical namespace path:

- `recap/client/base_client.py` composes REST queries with REST commands.
- `recap/client/permissions.py` exposes typed effective permissions.

Remote clients no longer need access to the server's SQLite path. Remote reads,
counts, and mutations use authenticated REST endpoints.

The client identity map canonicalizes models by stable entity family and UUID.
Later fuller loads upgrade existing canonical objects rather than creating a
second instance for the same entity.

## 8. Builder and Aggregate Submission Model

Builders preserve the existing fluent construction style without issuing
incremental remote writes.

- `recap/dsl/drafts.py` defines nested draft payloads.
- `recap/dsl/process_builder.py` accumulates process-template and process-run
  drafts and submits aggregate commands.
- `recap/dsl/resource_builder.py` accumulates resource-template and resource
  drafts and submits aggregate commands.
- `recap/client/base_client.py` selects the local or REST command executor.

The important invariant is one builder save equals one aggregate command. A
builder body that raises produces no persistence side effect. The same command
shape works locally and remotely, which is tested by
`recap/tests/test_remote_builder_parity.py`.

Resource and process-run loading use bounded, depth-independent SQL statement
budgets to avoid N+1 growth. `query.export(format, destination)` delegates to a
registered exporter; no export serialization format is part of core behavior.

## 9. Commit History by Milestone

The branch was implemented in these architectural phases:

### Namespace domain and visibility

- `fea45fa` `feat: add namespace lifecycle primitives`
- `a6a3520` `feat: add namespace domain model`
- `9c1a86e` `feat: migrate campaign data to namespaces`
- `d9a1071` `feat: scope provenance entities by namespace`
- `7d8d72a` `feat: query within namespace context`
- `9c701c4` `feat: freeze referenced provenance aggregates`
- `acb7b2c` `feat: copy frozen resources by namespace`
- `2b91a12` `feat: replace campaigns with namespaces`

### Authentication and authorization

- `38ad077` `feat: define authentication and scope contracts`
- `a75a757` `feat: authenticate requests with API key`
- `0235dd3` `feat: compile namespace authorization grants`
- `6c7bb3d` `feat: load authorization snapshot generations`
- `0457c00` `feat: authorize namespace operations`
- `f86ab70` `feat: add secure request context and audit`
- `da892a5` `feat: enforce namespace query policy`
- `ae75422` `feat: authenticate namespace REST client`

### Command and mutation infrastructure

- `41ab642` `feat: add request-scoped command spine`
- `a5087d8` `feat: add idempotent audited commands`
- `b133c4c` `feat: expose namespace write commands`
- `3ff2713` `feat: write process templates atomically`
- `45d627d` `feat: write resource templates atomically`
- `e2265ca` `feat: add resource copy-on-write API`
- `aba3322` `feat: write process runs through commands`
- `117e25e` `refactor: submit builders as aggregate commands`

### REST transport and cutover

- `9617590` `feat: add namespace REST client`
- `52d768c` `feat: route remote writes through REST`
- `8e33371` `Merge rest API implementation`

Fix and style commits in the same range preserve behavior introduced by these
milestones, including atomicity, metadata, delegation, and lint fixes:
`05754d7`, `4db1e95`, `11414b5`, `96c0b8e`, `16aac38`, and `a7af6da`.

## 10. Verification Surface

Branch-added or branch-modified tests cover the architecture at each boundary:

- Namespace migration, ownership, lifecycle, queries, and copy:
  `test_namespace_migration.py`, `test_namespace_ownership.py`,
  `test_lifecycle_operations.py`, `test_resource_copy.py`.
- Authentication and authorization: `test_authentication_models.py`,
  `test_request_authentication.py`, `test_request_security.py`,
  `test_authorization_compiler.py`, `test_authorization_snapshot.py`,
  `test_namespace_policy.py`, `test_rest_query.py`.
- Commands and consistency: `test_command_models.py`, `test_command_errors.py`,
  `test_idempotency.py`, `test_revisions.py`, `test_mutation_audit.py`,
  `test_request_backend_scope.py`.
- REST and client behavior: `test_rest_adapter.py`, `test_rest_namespaces.py`,
  `test_rest_process_templates.py`, `test_rest_resource_templates.py`,
  `test_rest_resources.py`, `test_rest_process_runs.py`,
  `test_namespace_client.py`, `test_remote_builder_parity.py`.

The final branch reports record full-suite verification at `518 passed, 1
skipped` before the final merge commit, with existing warnings called out in the
task reports. The relevant reports are `plan3-task-3-report.md`,
`plan3-task-6-report.md`, `plan3-task-7-report.md`, and
`plan3-task-9-report.md`.
