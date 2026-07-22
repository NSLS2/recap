# RECAP Query Optimisation Plan

> Living document. Serves as implementation memory. Update the **Findings &
> Progress Log** at the bottom as each step lands.

## Goal

Speed up the **read/query path** of RECAP (SQLAlchemy query construction +
Pydantic hydration) and give users concrete guidance for constructing fast
queries. Companion to `RECAP_LIBRARY_BUILDER_OPTIMISATION.md` (which covers the
**write/builder** path).

Original trigger: `beamx` GUI refresh profiling. Builder doc covers the
`dewar_upsert` / per-request write costs. This doc covers the query side:
`load="full"` resource trees, `get_resource(expand=True)`, `set_campaign()`
round-trips, and user query ergonomics.

---

## Query path map

```
QueryDSL.resources()/process_runs()      dsl/query.py    → builds QuerySpec (pure python, free)
  → backend.query(schema, spec)          local.py:1166
      → _build_select()                  local.py:1123   (SQL construction, joins, filters, CTE)
      → _relationship_loaders()          → resolve_loader_options()  query_loaders.py:160
                                            (selectinload chains via chain_load, loaders.py:4)
      → session.scalars(stmt).unique()                   ← THE DB round-trip(s)
      → Hydrator.construct_many()        resource_construct.py / process_run_construct.py
            model_construct (skip validation) + per-id caches
```

**Already well-optimised (do not touch):**
- Hydrators use `model_construct` (skips Pydantic validation) + per-instance
  id-keyed caches (`resource_construct.py:38-46`, `process_run_construct.py:44-55`).
- Dynamic param/property models are `lru_cache`d (`utils/dsl.py:207,243,270`).
- `chain_load` = nested `selectinload` (no cartesian-product row blow-up).

**Bottlenecks are SQL loader coverage + redundant round-trips, NOT Pydantic.**

---

## Findings (severity order)

### F1 — N+1 lazy load on `load="full"` resource queries  *(HOT, real bug)*
- `query_loaders.py:106` `(ResourceSchema,"children")` = `chain_load(Resource.children)`
  → loads **direct children ORM only**. No grandchildren, no child
  `properties`, no child `template`.
- Hydrator `resource_construct.py:262-284` recurses **all** descendants and
  touches `.properties` → one lazy SELECT per descendant.
- Dewar → 12 pucks → 96 samples ≈ **100+ lazy SELECTs** per resource.
- README already documents this (§"load=full and the hidden N+1", line 982).

### F2 — `get_resource(expand=True)` has the same N+1  *(not fixed by builder doc)*
- `local.py:574-580`: `chain_load(Resource.children, Resource.children)` loads
  2 child levels but never their `properties`/`template`.
- Builder doc Step 3 only *removes the call* from `ResourceBuilder.save()`. Any
  direct `get_resource(expand=True)` caller still hits the cascade.

### F3 — Same latent N+1 in ProcessRun resource subtree
- `include("resources")` preloads only `Resource.children`
  (`query_loaders.py:86-90`), but `ProcessRunSchemaHydrator` recurses
  `resource.children` (`process_run_construct.py:367-386`,
  `_construct_resource_schema` 345-348) → lazy load per descendant.

### F4 — `set_campaign()` round-trip on every call
- `local.py:167-172`: opens tx, `SELECT Campaign WHERE id=?`, every call — even
  when id unchanged. README §line 1012.

### F5 — `on_existing="silent"` = INSERT→ROLLBACK→SELECT (3 round-trips)
- Documented (README:1037). Workaround is the ID overload. **No code change
  planned** — doc-only guidance (covered by D).

### F6 — Recursive-CTE descendant query exists but underused
- `_build_select_resource` (`local.py:1032-1042`) already builds a recursive CTE
  for `under_parent`. `_descendant_templates_cte` (`resource.py:268-272`)
  exists for templates. The correct bulk-fetch machinery is present but not the
  default and not ergonomic in the DSL.

### Key structural fact
- `Resource.parent/children` is self-referential with **unbounded depth**
  (`resource.py:168-175`; `max_depth=10` only guards `__init__`, not queries).
  → Fixed-depth `chain_load(children, children, …)` (option A1) is fragile.
  → Recursive-CTE bulk fetch (option A2) is the correct general fix.

---

## Decisions (resolved with user)

| # | Decision |
|---|----------|
| A approach | **A2 — recursive-CTE bulk fetch + Python tree assembly.** Depth is unbounded; A1 (fixed-depth preload) breaks past N levels and still misses descendant props. Reuse existing CTE machinery (F6). |
| B approach | **B1 — route `get_resource(expand=True)` through the same bulk helper built in A.** Avoid B2's depth-capped preload (same A1 fragility). |
| F3 scope | **In scope.** Apply the same flat-hydration fix to `ProcessRunSchemaHydrator` resource subtrees so `include("resources")` is bounded too. |
| D naming | Add first-class `descendants()` helper to `ResourceQuery` **and** keep `under_parent()`. Update README. |
| Builder path (E) | Execute `RECAP_LIBRARY_BUILDER_OPTIMISATION.md` as written. Orthogonal to A–D. |

---

## Implementation steps

### Step 0 — Test infrastructure: statement counter  *(do first)*
Add a reusable fixture/helper in `recap/tests/` that counts SQL statements via
a SQLAlchemy `before_cursor_execute` event listener, e.g.:

```python
@contextmanager
def count_statements(engine_or_session):
    counter = {"n": 0}
    def _before(conn, cursor, statement, *a):
        counter["n"] += 1
    event.listen(target, "before_cursor_execute", _before)
    try:
        yield counter
    finally:
        event.remove(target, "before_cursor_execute", _before)
```

Used by every step below to assert **bounded** statement counts (the regression
guard that makes "N+1 fixed" verifiable rather than asserted).

**Files:** `recap/tests/conftest.py` (or a `recap/tests/helpers.py`).

---

### Step A — Fix `load="full"` / `include("children")` resource N+1 (F1)

**A.1 — Bulk descendant loader (backend).**
Add a helper on `LocalBackend` that, given a set of root resource ids, fetches
**all descendants in one recursive-CTE query** with these relationships eager
(`selectinload`): `Resource.template` (+`types`, +`attribute_group_templates…attribute_templates`),
`Resource.properties` (+`Property._values`, +`Property.template…attribute_templates`).
Recurse on `Resource.parent_id` (analogous to `_build_select_resource`'s CTE,
but selecting full `Resource` rows, not just ids).

**A.2 — Flat-list hydration (hydrator).**
Refactor `ResourceSchemaHydrator.construct_many` to accept the **flat list** of
all resources (roots + descendants) and assemble the parent→children map in
Python by `parent_id`, instead of walking `resource.children.values()` (which
re-triggers lazy load). `_construct_resource_schema` builds its `children` dict
from the pre-assembled map. Keep the existing per-id `_resource_cache`.

**A.3 — Wire into `query()`.**
In `local.py:1201-1219`, when `schema is ResourceSchema and include_children`:
fetch roots, call A.1 for descendants, pass the combined flat list to A.2.
When `include_children` is False, keep the current single-query path.

**Files:** `adapter/local.py`, `adapter/resource_construct.py`.
**Verify:** test asserts a 3-level tree query under `load="full"` issues a
**constant** number of SELECTs (≈2–3, independent of tree size); output equality
vs current hydration for a representative tree.

---

### Step B — Fix `get_resource(expand=True)` (F2)

Route `expand=True` through the A.1 bulk helper + A.2 flat hydration so the
returned tree has zero lazy loads. Preserve the public return contract
(`ResourceSchema` when `expand`, else `ResourceRef`).

**Files:** `adapter/local.py` (`get_resource`).
**Verify:** statement-count test on a 3-level tree via `get_resource(expand=True)`.

---

### Step C — `set_campaign()` short-circuit (F4)

`local.py:167`. Add identity guard at top:
```python
def set_campaign(self, id: UUID) -> CampaignSchema:
    if self._campaign is not None and self._campaign.id == id:
        return CampaignSchema.model_validate(self._campaign)
    ...
```
(Guard against `self._campaign` not existing yet — `LocalBackend.__init__`
doesn't set it; use `getattr(self, "_campaign", None)`.)

**Files:** `adapter/local.py`.
**Verify:** test that a 2nd `set_campaign(same_id)` issues **0** SQL statements.

---

### Step D — `descendants()` DSL helper + docs (F5, F6)

**D.1** Add `descendants()` to `ResourceQuery` (`dsl/query.py:340`):
```python
def descendants(self, parent, *, of_template=None) -> "ResourceQuery":
    q = self.under_parent(parent).include(["template", "properties"])
    if of_template is not None:
        q = q.filter(resource_template_id=of_template)
    return q
```
Wraps the README-recommended one-query bulk pattern.

**D.2** Update README Performance Guide:
- §`load="full"` → point at `descendants()`.
- Add a "Query recommendations" subsection (see below).

**Files:** `dsl/query.py`, `README.md`.
**Verify:** test that `descendants()` returns same rows as the manual
`under_parent().filter().include()` chain.

---

### Step E — Builder write-path

Execute `RECAP_LIBRARY_BUILDER_OPTIMISATION.md` verbatim:
`dsl/resource_builder.py` + `dsl/process_builder.py` (`_loaded_in_uow` /
`_model_dirty` flags, `__enter__` skip-guard, `save()` drops
`get_resource(expand=True)`, `set_params()` drops redundant reload+persist,
conditional `__exit__` persist, remove debug `print()`), **plus** that doc's
mandated regression test for `ResourceBuilder.add_child()`.

Independent of A–D. Can be parallelised.

---

## User-facing query recommendations (to land in README via D.2)

1. **Default to `shape="ref"` or `load="none"`** when you only need
   identity/scalar fields — skips relationship hydration entirely.
2. **Use `include([...])`** to load exactly the relations you touch; never
   `load="full"` on deep resource trees.
3. **Fetch deep hierarchies with `descendants()`** (or
   `under_parent()+filter(resource_template_id=)`) — one bulk query, then call
   `build_property_model()` per row.
4. **Known-existing records → ID overloads** (`resource_id=`/`process_run_id=`),
   not `on_existing="silent"` (3 round-trips, F5).
5. **Group writes by campaign**, call `set_campaign()` once per group (also
   short-circuited by C).
6. **`include(...)` is only valid with `shape="schema", load="none"`** — raises
   `ValueError` with `load="full"`.

---

## Sequencing

1. **Step 0** — statement-counter test helper.
2. **Step C** — isolated, trivial, immediate win.
3. **Step A** — core bulk-CTE + flat hydration.
4. **Step B** — reuse A's helper.
5. **Step F3** — apply flat hydration to ProcessRun resource subtree.
6. **Step D** — ergonomics on top of A.
7. **Step E** — independent write-path (parallelisable).

## Verification

- `count_statements` assertions for A/B/C/F3 (bounded SELECT counts).
- Output-equality tests before/after for representative trees.
- Full suite: `pixi run -e dev test`.
- Lint/format: `pixi run -e dev lint`.

---

## Findings & Progress Log

> Append entries as work lands. Newest at top.

- **Follow-up F (include-properties template gap) — DONE.** `include(["properties"])`
  loaded `Property._values` but not `Property.template`, so
  `build_property_model()` (and the `PropertySchema` hydrator) lazy-loaded each
  property group's template — an N+1 over property groups. The process-run
  `(ProcessRunSchema, "resources")` loader and `_resource_subtree_loaders()`
  already eager-load both; standalone `include(["properties"])` did not.
  - **`recap/adapter/query_loaders.py`:** both `(ResourceSchema, "properties")`
    and `(ResourceRef, "properties")` entries now eager-load
    `Property._values → AttributeValue.template` **and**
    `Property.template → AttributeGroupTemplate.attribute_templates` (mirrors the
    process-run resources loader). Added `AttributeValue` to the
    `recap.db.attribute` import.
  - **Tests:** `recap/tests/test_include_properties_perf.py` (2) —
    (1) `build_property_model()` after `include(["properties"])` issues **0**
    additional SQL; (2) statement count ≤ the `load="full"` path. **RED
    confirmed**: pre-fix `build_property_model()` lazy-loaded per group.
  - **Known residual (out of scope, pre-existing):** `Property._values` uses
    `mapped_collection(lambda av: av.template.name)`, so populating the
    `_values` selectin batch keys each value by `av.template.name`, emitting one
    `attribute_group_template.id = ?` load **per distinct property group**. This
    is bounded (per distinct template, not per resource) and is present
    identically in the `load="full"` / `include_resources()` paths — so this fix
    brings `include(["properties"])` to **parity** with the documented-efficient
    path, which was the goal. A deeper fix (e.g. `lazy="selectin"` on
    `AttributeValue.template`, or a non-template-keyed collection) would remove
    the residual across all paths; tracked separately.
  - **Verify:** full suite **150 passed** (148 + 2 new); ruff clean.

- **Step E — DONE.** Executed `RECAP_LIBRARY_BUILDER_OPTIMISATION.md` (builder
  write-path). See that doc's top-of-file STATUS block for full notes.
  - **`recap/dsl/resource_builder.py`:** `_loaded_in_uow` flag; `__enter__` skips
    reload when fresh; `save()` drops the post-commit `get_resource(expand=True)`
    cascade (single `commit()`); `_restart_uow()` resets flag; debug `print()`
    removed.
  - **`recap/dsl/process_builder.py`:** `_loaded_in_uow` + `_model_dirty` +
    `_params_flushed` flags; `__enter__` skip-guard; `set_params()` drops
    redundant reload+persist (sets `_params_flushed`); `set_model()` /
    `assign_resource()` set `_model_dirty`; `__exit__` persists only when
    `_model_dirty`, reloading first only when also `_params_flushed`; `save()` /
    `_restart_uow()` reset flags; `_handle_existing_process_run()` sets
    `_loaded_in_uow` after recovery load.
  - **Plan delta:** the unconditional `__exit__` reload-before-persist from the
    Scenario C callout clobbers `set_model()` mutations, so it was narrowed to
    fire only when a model mutation is combined with `set_params()` (new
    `_params_flushed` flag).
  - **Tests:** added mandated `add_child()` regression test
    (`test_builders_models.py::test_resource_builder_add_child_persists_and_links`)
    — written + confirmed passing pre-refactor, kept green through. Updated
    `test_process_run_update_persists_param_changes` to use `get_model()` /
    `set_model()` (in-place process-run schema mutation w/o a setter is no longer
    auto-persisted under the new flag model).
  - **Verify:** full suite **148 passed**; ruff clean.
  - **All optimisation steps (0, C, A, B, F3, D, E) now complete.**

- **Step D — DONE.** Added the `descendants()` ergonomic helper + README docs.
  - **`recap/dsl/query.py`:** `ResourceQuery.descendants(parent, *,
    of_template=None)` — wraps `under_parent(parent).include(["template",
    "properties"])`, with optional `filter(resource_template_id=of_template)`.
    Reuses `under_parent`'s existing recursive-CTE (all descendants, any depth),
    so it's one bulk query.
  - **`README.md`:** (1) new **"Query recommendations"** checklist at top of
    Performance Guide; (2) `descendants()` example added to the property-filter
    `under_parent` section; (3) Performance Guide `load="full"` subtree section
    now recommends `descendants()`; (4) `load="full"` Note updated to mention
    `descendants()`.
  - **Tests:** `recap/tests/test_query_dsl.py` (3) — `descendants()` returns same
    rows as the manual `under_parent().include([...])` chain (all levels);
    `of_template` narrows to one template; template + properties eagerly
    hydrated. **RED confirmed**: `AttributeError: 'ResourceQuery' object has no
    attribute 'descendants'`.
  - **Verify:** full suite **147 passed** (144 + 3 new); `ruff` clean;
    `mkdocs build --strict` clean (README docs valid).
  - **Next:** Step E — builder write-path (`RECAP_LIBRARY_BUILDER_OPTIMISATION.md`
    verbatim). Independent of A–D.

- **Step F3 — DONE.** `include("resources")` / `load="full"` on a **process run**
  query now hydrates each assigned resource's full subtree with a bounded,
  depth-independent number of SELECTs (was N+1 scaling with the assigned
  resource's tree depth). F3a (threading the CTE through the assignment loader)
  was **not** invasive — no re-plan needed.
  - **`recap/adapter/process_run_construct.py`:**
    `ProcessRunSchemaHydrator._construct_resource_schema` gained an optional
    `children_map` param — builds children from the pre-assembled
    `parent_id → [child]` map instead of lazy `resource.children.values()`.
    Threaded through `construct_many` → `_construct_process_run_schema` → both
    assignment loops (run-level `run.assigned_resources` **and** step-level
    `step.resources`).
  - **`recap/adapter/local.py`:** two static helpers next to
    `_load_resource_subtrees`: `_assigned_resource_root_ids(runs)` (collects
    run-level + step-level assigned resource ids, deduped) and
    `_build_children_map(flat_resources)` (`parent_id → [child]`). `query()`
    ProcessRunSchema branch: when `include_resources`, materialise runs, collect
    root ids, `_load_resource_subtrees` once, build map, pass `children_map` into
    `construct_many`.
  - **`recap/adapter/query_loaders.py`:** removed the now-redundant one-level
    `chain_load(ProcessRun.assignments, ResourceAssignment.resource,
    Resource.children)` from the `(ProcessRunSchema, "resources")` loader (the
    CTE supplies the whole subtree). `Resource` import still used elsewhere.
  - **Tests:** `recap/tests/test_process_run_resource_tree_perf.py` (2). Seeds a
    process run (ORM, via `make_query`/`db_session`) with one assigned resource
    rooting a linear child chain. (1) depth-independence: 3-level == 4-level
    statement count. (2) structure: full chain hydrated with correct names +
    child keys. **RED confirmed**: pre-fix 3-level=26 vs 4-level=28 (+2/level).
  - **Verify:** full suite **144 passed** (142 + 2 new), no regressions
    (existing `test_query_dsl.py` `include_resources` / `test_platemate_client.py`
    process-run paths still pass → output-equality preserved). `ruff` clean.
  - **No README change:** internal; `include("resources")` / `load="full"` API +
    returned schemas unchanged.
  - **Next:** Step D (`descendants()` DSL helper + README), then Step E
    (independent builder write-path).

- **Client API: `RecapClient.get_resource(...)` added.** `get_resource` was a
  **backend** method only; tests reached into `client.backend.get_resource(...)`,
  which breaks under a future REST backend (no in-process backend object to grab).
  - **`recap/client/base_client.py`:** added `get_resource(name, template_name,
    template_version="1.0", *, expand=False)` — thin wrapper delegating to
    `self.backend.get_resource(...)` (already in the `Backend` Protocol, so REST
    backends implement it too). `@overload`s: `expand=False → ResourceRef`,
    `expand=True → ResourceSchema`. `expand` is **keyword-only** on the client
    (clean overloads). Imports `ResourceRef`.
  - **Tests de-coupled from backend:** `test_get_resource_perf.py` (2 sites) and
    `test_platemate_client.py` (7 `get_resource` sites) now call
    `client.get_resource(...)`. Safe inside `backend.begin()` UoW blocks:
    `_session_scope()` reuses the active UoW session when one exists
    (`local.py:131`), so behaviour is identical.
  - **Still backend-coupled (separate issue, NOT fixed):** `test_platemate_client.py`
    `client.backend.begin()` / `client.backend.create_resource()` /
    `client.backend.get_resource_template()`, and `test_template_guards.py`
    `client.backend.session.get(...)`. No client-level UoW/template-load
    equivalents yet — defer to a dedicated cleanup.
  - **README:** new `#### Loading an existing resource` subsection under
    Resources documents `client.get_resource(..., expand=…)`.
  - **Verify:** full suite **142 passed**; `ruff check` + `ruff format` clean.

- **Step B (F2) — DONE.** `get_resource(expand=True)` now reuses the Step A
  recursive-CTE bulk loader; subtree hydration is bounded + depth-independent
  (was a depth-capped `chain_load(children, children)` → N+1 / `DetachedInstanceError`
  past 2 levels).
  - **B.1** `recap/adapter/local.py` `get_resource` (was lines 573–603): dropped
    the `expand=True` `chain_load(...)` block. Now resolves the root row via the
    existing `load_single(session, stmt, label="Resource")`, then **inside the
    same `_session_scope()`**: `flat = _load_resource_subtrees(session,
    [resource.id])` → `ResourceSchemaHydrator().construct_tree(flat, [resource.id],
    include_template=True, include_properties=True, full=True, on_unloaded="warn")`
    → returns `trees[0]`. `expand=False` branch unchanged
    (`ResourceRef.model_validate`). `not-found` behaviour preserved (`load_single`
    still raises with `label="Resource"`). `chain_load` import still used elsewhere
    (30 refs) — kept.
  - **Tests:** `recap/tests/test_get_resource_perf.py` (2). (1) depth-independence:
    3-level == 4-level statement count via `client.backend.get_resource(...,
    expand=True)`; (2) output-equality: returned tree (ids, names, `.children`
    keys, sample `properties.details.serial.value`) matches
    `query_maker(unscoped=True).resources(load="full").filter(name=...).first()`.
    **RED confirmed**: pre-fix path raised `DetachedInstanceError` (lazy
    `AttributeGroupTemplate.attribute_templates` after session close) — the
    depth-cap N+1 manifesting.
  - **Note:** `get_resource` is a **backend** method (`client.backend.get_resource`),
    not exposed on `RecapClient`. Tests call it via `client.backend`.
  - **Verify:** full suite `pixi run -e dev test` = **142 passed** (140 + 2 new),
    no regressions. `ruff check` + `ruff format` clean (no reformat).
  - **No README change:** internal; `get_resource(expand=True)` signature +
    returned `ResourceSchema` unchanged.
  - **Next:** Step F3 (`process_run_construct.py:345-348`).

- **Step A (F1) — DONE.** `load="full"` / `include("children")` resource queries
  now hydrate the whole tree with a bounded, **depth-independent** number of
  SELECTs (was N+1 scaling with tree size).
  - **A.1** `recap/adapter/local.py`: `_load_resource_subtrees(session, root_ids)`
    — recursive-CTE fetch of roots + all descendants in one query, plus
    `_resource_subtree_loaders()` (a fixed `selectinload` set covering every
    relationship the hydrator touches: `parent`; `properties`→`_values` and
    `template.attribute_templates`; `template`→`types`, `parent.types`,
    `children`, `attribute_group_templates.attribute_templates`). Returns the
    flat list.
  - **A.2** `recap/adapter/resource_construct.py`: `_construct_resource_schema`
    gained a `children_map` param — iterates a pre-assembled `parent_id → [child]`
    map instead of lazy `resource.children.values()` (the N+1 source). New
    `construct_tree(flat_resources, root_ids, …)` builds the map and hydrates
    roots in order. `_resource_cache` / `set_loaded_relations` /
    `_post_build_dynamic_models` unchanged.
  - **A.3** `recap/adapter/local.py` `query()`: `ResourceSchema + include_children`
    routes root-ids query → A.1 → `construct_tree`. Also **skips the redundant
    one-level relationship loaders** on the root query for this path (it was
    double-loading: eager root batches *and* per-node lazy loads).
  - **Tests:** `recap/tests/test_resource_tree_perf.py` (2). Depth-independence:
    3-level == 4-level statement count (pre-fix 19 vs 22). Bounded count: 5-node
    chain = **16** stmts, depth-flat (pre-fix 25, one-per-node). The
    `resource_tree_path` loader-skip also dropped the bounded case 26 → 16.
  - **Verify:** full suite `pixi run -e dev test` = **140 passed** (no
    regressions → existing `include("children")`/`load="full"` resource tests
    confirm output-equality). `ruff check` + `ruff format` clean.
  - **No README change:** purely internal; the `load="full"` / `include(...)`
    public API and returned schemas are unchanged. The Performance Guide's
    existing advice (prefer `descendants()` / bounded includes over deep
    `load="full"`) still holds; deep `load="full"` is now merely *less* costly,
    not free.
  - **Next:** Step B (`get_resource(expand=True)` reuses A.1), then F3.

- **Step B — READY TO IMPLEMENT (exact plan for fresh session).**
  Reuse the Step A bulk loader inside `get_resource(expand=True)`.
  - **Current code** `recap/adapter/local.py` `get_resource` (lines **573–603**):
    builds `select(Resource).join(Resource.template).where(name, template.name,
    template.version, active)`. On `expand=True` it applies a **depth-capped**
    eager set — `chain_load(Resource.children, Resource.children)` (only **2**
    levels), `chain_load(Resource.template)`,
    `chain_load(Resource.children, Resource.properties)`,
    `chain_load(Resource.properties)` — then `load_single(...)` and
    `ResourceSchema.model_validate(resource)`.
  - **Bugs in current path:** (1) tree deeper than 2 levels → `model_validate`
    walks lazy `.children.values()` past the eager cap → N+1 *and* depth-capped
    (3rd-level children present but their grandchildren lazy-load). (2) Does not
    reuse `_load_resource_subtrees` / `construct_tree` from Step A, so output may
    differ subtly from the `query()` path.
  - **Change (B.1):** keep the initial `select` but fetch **root id only** (or
    the root row), then:
    1. resolve the single root `Resource.id` (raise/label "Resource" if missing,
       preserving `load_single` not-found behaviour);
    2. call `flat = _load_resource_subtrees(session, [root_id])` (Step A bulk
       CTE loader — already returns root + all descendants with templates +
       properties eagerly loaded);
    3. `trees = construct_tree(flat, [root_id], …)` (same hydrator the `query()`
       branch uses) and return `trees[0]`.
    Drop the `chain_load(...)` block entirely for `expand=True`. The `expand=False`
    branch is unchanged (still returns `ResourceRef.model_validate`).
    - Mind the session scope: current code does `load_single` inside
      `with self._session_scope() as session:` then `model_validate` **outside**
      it. `_load_resource_subtrees` + `construct_tree` must run **inside** the
      same `session` scope (hydration touches relationships). Match the
      `query()` ResourceSchema branch structure (~`local.py:1266`).
    - Check `construct_tree` signature/required kwargs in
      `recap/adapter/resource_construct.py` (it takes the flat list + root_ids;
      confirm whether `_resource_cache` / dynamic-model post-build args are
      needed — copy exactly what the `query()` branch passes).
  - **Test (B RED first):** new `recap/tests/test_get_resource_perf.py`. Build a
    ≥3-level resource chain (reuse the builder pattern from
    `test_resource_tree_perf.py`). Assert `get_resource(name, tmpl, expand=True)`
    issues a **bounded, depth-independent** statement count via the
    `count_statements` fixture (compare 3-level vs 4-level chain → equal). RED:
    current depth-cap path will diverge / exceed. Also assert **output-equality**:
    returned `ResourceSchema` tree (ids, names, `.children` keys, a sample
    `.properties` value) matches what `query_maker(...).resources(load="full")`
    returns for the same root, so the refactor is behaviour-preserving.
  - **GREEN:** implement B.1; both perf + equality tests pass; full suite
    (`pixi run -e dev test`) stays green; `ruff check` + `ruff format` clean.
  - **Then:** Step F3 (`process_run_construct.py:345-348` — `children_map` on the
    ProcessRun hydrator + source subtree via CTE; **pause + re-plan F3 if F3a
    threading the CTE through the assignment loader proves invasive** — no
    interim depth-bump fallback).

- **Steps A + B + F3 — investigation complete, implementation starting (A first).**
  - **Confirmed shared root cause (N+1).** All three paths hydrate resource
    trees by walking `resource.children.values()` in Python. The
    `(ResourceSchema, "children")` eager-loader is **one level only**
    (`query_loaders.py:106` = `chain_load(Resource.children)`), so every node
    below depth 1 lazy-loads its `children`/`properties`/`template` → SELECT
    count scales with tree size.
    - **F1 (A):** `query()` `load="full"`/`include("children")` →
      `ResourceSchemaHydrator._construct_resource_schema`
      (`resource_construct.py:262-273`).
    - **F2 (B):** `get_resource(expand=True)` (`local.py:590-601`) bypasses the
      hydrator entirely — uses `chain_load(Resource.children, Resource.children)`
      (depth-capped at 2) + `ResourceSchema.model_validate` (Pydantic recursion
      lazy-loads past depth 2).
    - **F3:** `ProcessRunSchemaHydrator._construct_resource_schema`
      (`process_run_construct.py:345-348`) for `include("resources")`.
  - **Existing CTE precedent:** `_build_select_resource` (`local.py:1048-1058`)
    already builds a recursive descendant-**id** CTE, but selects ids only and
    excludes the root. `chain_load` = nested `selectinload`
    (`recap/utils/loaders.py`).
  - **Plan (A2/B1, flat hydration):**
    - **A.1 — bulk descendant loader** (`local.py`): new
      `_load_resource_subtrees(session, root_ids) -> list[Resource]`. Recursive
      CTE over `Resource` (root_ids → `parent_id`, **including roots**), single
      `select(Resource).join(cte)` with `selectinload` mirroring the full-load
      set: `template`(+`types`, +`attribute_group_templates.attribute_templates`,
      +`parent.types`), `properties`(+`_values`, +`template.attribute_templates`),
      and `parent` (for `_construct_resource_ref`). Returns a flat list; SELECT
      count bounded + **depth-independent**.
    - **A.2 — flat hydration** (`resource_construct.py`): new `construct_tree`
      builds `children_by_parent` from the flat list; `_construct_resource_schema`
      gains an optional `children_map` and iterates that instead of lazy
      `resource.children.values()`. Keep `_resource_cache`,
      `set_loaded_relations`, `_post_build_dynamic_models`.
    - **A.3 — wire `query()`** (`local.py:1217-1235`): when
      `ResourceSchema and include_children`, run the (filtered) root query for
      ids, call A.1, feed flat list to `construct_tree`; **drop** the one-level
      `chain_load(Resource.children)` from that path. `include_children=False`
      unchanged.
    - **B** (`local.py:573-603`): rewrite `get_resource(expand=True)` to use A.1
      + hydrator (single root id). `expand=False` unchanged (`ResourceRef`).
    - **F3** (`process_run_construct.py`): same `children_map` treatment; source
      subtree rows via CTE (**F3a**). If F3a requires invasive changes to the
      assignment loader → **pause and re-plan F3** (no interim depth-bump
      fallback; A1 fragility rejected).
  - **Execution:** land A → B → F3 sequentially, each its own RED→GREEN→verify
    cycle, checkpointed between.
  - **Tests:** new `recap/tests/test_resource_tree_perf.py` — output-equality vs
    a pre-change snapshot + `count_statements` assertion that the count is a
    small constant **and identical for a 3-level vs 4-level tree**
    (depth-independence = the real N+1 guard).
  - **Risks:** equality contract (`model_construct` + dynamic `build_property_model`
    — snapshot before editing); `_resource_cache`/parent-cycle safety (A.1 eager-
    loads `parent`); campaign-scope + property-filter joins must still apply to
    the **root** query (A.1 only expands already-filtered roots); F3 scope creep.
- **Latent bug: campaign metadata dropped on create — FIXED.**
  - **Root cause:** `LocalBackend.create_campaign` built `Campaign(metadata=metadata)`
    (`recap/adapter/local.py:161`). `metadata` is **not** a mapped attribute on
    `Campaign` (the column is `meta_data`); SQLAlchemy's declarative `__init__`
    silently set a throwaway instance attribute and never persisted it, so every
    campaign created with metadata ended up with `meta_data = None`. Confirmed by
    repro: `Campaign(metadata={'k':'v'}).meta_data is None`.
  - **Scope:** create-only. Read path (`CampaignSchema.meta_data` ↔ column
    `meta_data`) and `update_campaign` (writes `meta_data`) were already correct.
    No migration change (DB column is already `meta_data`).
  - **Fix:** one line — `Campaign(metadata=metadata)` → `Campaign(meta_data=metadata)`.
    Public API keyword stays `metadata` on `client.create_campaign`, the `Backend`
    Protocol, and `LocalBackend.create_campaign` — no breaking change.
  - **Decision:** no `__init__` guard against unknown kwargs (rely on the
    regression test instead). Noted naming inconsistency (`metadata` param vs
    `meta_data` schema field / `update_campaign` kwargs) left for a future
    non-breaking alignment pass.
  - **Tests:** `recap/tests/test_campaign_update.py` +1
    (`test_create_campaign_persists_metadata`: asserts both the in-process return
    value and a forced DB reload carry `meta_data`). RED confirmed (`None`), then
    GREEN.
  - **Verify:** new test GREEN. Full suite `pixi run -e dev test` = **138 passed**.
    `ruff check` + `ruff format --check` clean on touched files.
- **`force` reload + `update_campaign` path — DONE.**
  - **`force` escape hatch (client-only).** `RecapClient.set_campaign` gained
    `*, force: bool = False`. The client cache short-circuit is now bypassed
    when `force=True`, re-querying the backend (for out-of-band edits). The
    backend `set_campaign` guard added in Step C was **removed** — the backend
    must never short-circuit (a REST backend's campaign endpoint always returns
    fresh data; caching is a client concern, and the guard would otherwise
    block the client's `force=True` since the ids match). Confirmed the only
    caller of `backend.set_campaign` is the client.
  - **No automatic staleness detection** (rationale, rejected): detecting
    staleness needs a DB/REST round-trip — exactly what the cache avoids — and
    even `Campaign.modified_date` (exists via `TimestampMixin`) can't be read
    without that round-trip, nor can it observe cross-process edits for free.
    Explicit `force=True` is the chosen contract.
  - **New `update_campaign` write path** (did not exist before). Follows the
    campaign UoW convention (client owns `begin()`/`commit()`), not the
    `_session_scope`/`owns_tx` style of `update_resource`.
    - `recap/adapter/local.py`: `update_campaign(self, campaign)` SELECTs by id
      (raise `ValueError` if missing), full-overwrites the 4 writable columns
      (`name`, `proposal=str(...)`, `saf`, `meta_data`), sets `self._campaign`,
      `flush()`, returns validated schema. DB enforces
      `uq_campaign_name_proposal`.
    - `recap/adapter/__init__.py`: added `update_campaign` to `Backend`
      Protocol.
    - `recap/client/base_client.py`: `update_campaign(campaign=None, **fields)`
      — accepts an explicit schema OR field kwargs (applied via `model_copy`),
      defaults to the active campaign, rejects unknown kwargs with `TypeError`
      and no-target with `ValueError`. Refreshes the client cache on success.
  - **Public `campaign` read accessor** added (`@property`) so users never
    touch `_campaign`. `create_campaign`/`set_campaign` now **return** the
    active `CampaignSchema` (was `None`; additive).
  - **Tests:** `test_set_campaign_perf.py` +1
    (`test_set_campaign_force_reloads_issues_sql`, force → >0 SQL, id
    unchanged). New `test_campaign_update.py` (8 tests: return values, `campaign`
    property, update via kwargs/schema, cache refresh, `ValueError`/`TypeError`).
  - **Verify:** new tests GREEN (12 in the two files). Full suite
    `pixi run -e dev test` = **137 passed**. `ruff check` + `ruff format --check`
    clean on touched files (used `ruff` directly; `pixi run -e dev lint` still
    broken — see note below).
- **Step 0 + Step C — DONE.**
  - `recap/tests/conftest.py`: added `count_statements(target)` context manager
    (listens on `target.engine` `before_cursor_execute`) + `statement_counter`
    fixture. Verified it correctly counts: pre-fix `set_campaign(same_id)` = 2
    statements.
  - `recap/tests/test_set_campaign_perf.py`: 3 tests — same-id → 0 SQL, schema
    arg → 0 SQL, different-id → >0 SQL.
  - `recap/client/base_client.py` `set_campaign`: resolve target id up front,
    early-return (skip `begin()`/SELECT/`commit()`) when
    `self._campaign.id == target_id`. This is where the real round-trip is
    saved (the client opened a tx around the backend call).
  - `recap/adapter/local.py` `set_campaign`: defensive guard for direct backend
    callers via `getattr(self, "_campaign", None)` (backend stores an ORM
    `Campaign`; `model_validate` on the already-loaded columns is safe).
    **SUPERSEDED:** this backend guard was later removed (see the `force` reload
    entry above) — the backend must never short-circuit.
  - **Verify:** `test_set_campaign_perf.py` GREEN (3 passed). Full suite
    `pixi run -e dev test` = 128 passed. `ruff check` + `ruff format --check`
    clean on touched files.
  - **Note:** `pixi run -e dev lint` fails with `pre-commit: command not found`
    (pre-commit not installed in the `dev` env); ran `ruff` directly instead.
    Flag for env fix later.
- *(not started)* — Plan written. Awaiting implementation kickoff.
