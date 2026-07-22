# Recap Library Builder Optimisation Plan

> **STATUS: DONE.** All steps implemented in `dsl/resource_builder.py` and
> `dsl/process_builder.py`. Full suite **148 passed**, ruff clean.
>
> **Implementation notes / deltas from this plan:**
> - **`__exit__` reload guard narrowed.** The plan's Step 5 `__exit__` does a
>   plain `if _model_dirty: persist()`, while the Scenario C "Potential concern"
>   callout adds a reload-before-persist. Reloading on *every* `_model_dirty`
>   exit clobbers `set_model()` pydantic mutations (the reload re-reads the DB,
>   discarding the in-memory changes before `persist()`). Fix: added a separate
>   `_params_flushed` flag (set in `set_params()`, reset in `save()` /
>   `_restart_uow()`). `__exit__` reloads **only when both** `_model_dirty` and
>   `_params_flushed` are true — i.e. a model mutation combined with
>   `set_params()` in the same block (Scenario C). `set_model()`-only and
>   `set_params()`-only paths are unaffected; the hot path stays reload-free.
> - **Test contract change.** `test_process_run_update_persists_param_changes`
>   previously mutated the run's pydantic schema **in place** with no setter and
>   relied on the old unconditional `__exit__.persist()`. Under the new model,
>   pydantic mutations must be handed back via `set_model()` to be persisted.
>   Updated the test to `get_model()` → mutate → `set_model()` (documented
>   Scenario B). In-place schema mutation without a setter is no longer
>   auto-persisted for process runs. (Resource builder `__exit__` remains
>   unconditional, so `ResourceBuilder` in-place mutation still persists.)
> - **`add_child()` regression test added** as mandated:
>   `recap/tests/test_builders_models.py::test_resource_builder_add_child_persists_and_links`
>   — parent builder + `add_child` + clean `__exit__`, asserting the child
>   resource is persisted and linked into the parent's `children`. Written and
>   confirmed passing against the *pre-refactor* code first (locks the path),
>   then kept green through the refactor.

## Background and motivation

### Observed problem

During profiling of the `beamx` GUI refresh cycle, per-phase timing was added
to every step of `RecapLocalStore._sync()` and `_read_tree_data()`. The results
for a single refresh on a local `/tmp/` SQLite database (tmpfs — no disk I/O
latency) were:

```
dewar_upsert   =  47–67 s   ← one build_resource call for a single resource
puck_prefetch  =  32 s      ← see beamx plan; not a recap builder issue
writes         = 129 s      ← 258 changed requests × ~0.5 s each
TOTAL          = 212 s
```

The 47–67 s on a single `build_resource` call and the ~0.5 s per
`build_process_run` call on a tmpfs database are not explainable by SQLite I/O
costs. The bottleneck is entirely in Python-side schema hydration caused by
**redundant database round-trips inside the builder context managers**.

### Context established by code inspection

The following facts were confirmed by reading the recap source:

1. **`ResourceBuilder.save()` calls `get_resource(expand=True)` after every
   commit** — `resource_builder.py:186-196`:
   ```python
   def save(self):
       self._ensure_uow()
       self._uow.commit(clear_session=False)
       self._resource = self.backend.get_resource(   # ← problem
           self.name, self.template_name,
           self.template_version, expand=True,
       )
       self._uow.end_session()
       self._uow = None
       return self
   ```
   `get_resource(expand=True)` in `local.py:557-587` issues a query with:
   ```python
   chain_load(Resource.children, Resource.children),  # grandchildren
   chain_load(Resource.children, Resource.properties), # children's properties
   chain_load(Resource.properties),
   ```
   For the dewar resource this loads all pucks (children) and all samples
   (grandchildren). Puck and sample properties are then lazy-loaded one-by-one
   during `ResourceSchema.model_validate()`, producing O(N_pucks + N_samples)
   lazy SELECT statements. At 8 ms per lazy load: 12 pucks + 96 samples =
   ~108 lazy loads × ~8 ms = ~864 ms for the schema construction alone. This
   accounts for most of the 47–67 s (the rest is repeated transaction overhead
   from multiple UoW open/close cycles).

2. **Both builders' `__enter__` unconditionally reloads the entity** even when
   `__init__` just loaded it moments earlier in the same transaction —
   `resource_builder.py:155-162` and `process_builder.py:373-386`:
   ```python
   def __enter__(self):
       self._ensure_uow()
       if self._resource is not None:
           self._resource = self._reload_resource(self._resource.id)  # always
           ...
   ```
   For the `process_run_id=` path used for known-existing runs, `__init__`
   calls `_load_existing_process_run()` → `_reload_process_run()` (1 SELECT
   with preloads), then `__enter__` immediately calls `_reload_process_run()`
   again (2nd SELECT with preloads). This is always wasted work when entering
   a builder that was constructed in the same call.

3. **`ProcessRunBuilder.set_params()` reloads and persists redundantly** —
   `process_builder.py:469-477`:
   ```python
   def set_params(self, filled_params: type[BaseModel]):
       self._ensure_uow()
       self.backend.set_params(filled_params)   # ← already flushes to session
       self._process_run = self._reload_process_run(...)  # 3rd SELECT
       self.persist()                            # 1st update_process_run
       return self
   ```
   `backend.set_params()` in `local.py:766-783` ends with `self.session.flush()`
   — the parameter values are already in the DB within the current transaction.
   The subsequent `_reload_process_run()` + `persist()` re-read what was just
   written, and then `__exit__` calls `persist()` again — writing the same data
   a second time:
   ```python
   def __exit__(self, exc_type, exc, tb):
       if exc_type is None:
           self.persist()    # 2nd update_process_run — same data as set_params()
           self.save()
   ```

4. **`update_process_run()` and `set_params()` write the same data** — both
   load step parameters from the DB, update `AttributeValue` entries, and
   flush. Calling both in sequence is entirely redundant for the common
   `set_params()`-only usage pattern.

5. **`ProcessRunBuilder.set_params()` requires a reload before `persist()`
   because `_process_run` (pydantic) doesn't reflect the ORM flush** — after
   `backend.set_params()` flushes, the in-memory pydantic `_process_run` still
   holds pre-`set_params()` parameter values. If `persist()` is called without
   reloading, `update_process_run()` would read the stale pydantic schema and
   overwrite the just-flushed values — a correctness bug. The reload exists to
   prevent this. The correct fix is to eliminate the redundant `persist()` call
   from `__exit__` when `set_params()` was the only mutation, not to keep the
   reload.

6. **A debug `print()` statement fires on every child resource creation** —
   `resource_builder.py:60-62`:
   ```python
   if self.parent_resource:
       print(f"adding child {self._resource.name} to {self.parent_resource.name}")
   ```
   This appears to be leftover development output. In production it emits to
   stdout for every sample (96 per session) and every puck (12 per session)
   created.

### Per-request cost breakdown (existing run, `set_params()` only)

| Step | DB operations |
|---|---|
| `__init__` (`_load_existing_process_run`) | 1 SELECT with `preloads=["steps","steps.parameters","resources"]` |
| `__enter__` (unconditional reload) | 1 SELECT with same preloads |
| `set_params()` — `backend.set_params()` | SELECT step + N attribute UPDATEs + flush |
| `set_params()` — `_reload_process_run()` | 1 SELECT with preloads |
| `set_params()` — `persist()` | SELECT step + N attribute UPDATEs + flush |
| `__exit__` — `persist()` | SELECT step + N attribute UPDATEs + flush |
| `__exit__` — `save()` | COMMIT |
| **Total** | **3 preloaded SELECTs + 3 update_process_run calls + 1 COMMIT** |

Target after fixes:

| Step | DB operations |
|---|---|
| `__init__` | 1 SELECT with preloads |
| `__enter__` | **0** — skipped |
| `set_params()` | SELECT step + N attribute UPDATEs + flush (unchanged) |
| `__exit__` | **0 persist** + COMMIT |
| **Total** | **1 preloaded SELECT + flush + COMMIT** |

---

## Implementation steps

All changes are in two files: `dsl/resource_builder.py` and
`dsl/process_builder.py`. No changes to `adapter/local.py` are required.

---

### File 1 — `dsl/resource_builder.py`

#### Step 1 — Add `_loaded_in_uow` flag

Add `self._loaded_in_uow: bool = False` to `__init__` before the
`try:` block. Set it to `True` immediately after the load/create succeeds,
and before `add_child_resources` is called:

```python
def __init__(self, ...):
    ...
    self._loaded_in_uow: bool = False
    ...
    try:
        if resource_id is not None:
            self._load_existing_resource(resource_id)
        else:
            self._create_or_reuse_resource()
        self._loaded_in_uow = True   # mark resource as fresh in this UoW
        if self.parent_resource:
            # debug print removed — see Step 5
            self.backend.add_child_resources(self.parent_resource, [self._resource])
    except Exception:
        if self._uow:
            self._uow.rollback()
            self._uow = None
        raise
```

#### Step 2 — `__enter__`: skip reload when already fresh

Replace the unconditional reload with a guarded reload. Only reload when
the builder is re-entered after a `save()` (i.e., `_loaded_in_uow is False`):

```python
def __enter__(self):
    self._ensure_uow()
    if self._resource is not None and not self._loaded_in_uow:
        # Re-entering after save() or _restart_uow() — reload to get current state
        self._resource = self._reload_resource(self._resource.id)
        self.name = self._resource.name
        self.template_name = self._resource.template.name
        self.template_version = self._resource.template.version
    return self
```

#### Step 3 — `save()`: remove post-commit `get_resource(expand=True)`

The current `save()` calls `get_resource(expand=True)` after the commit, which
triggers the recursive child-loading cascade. The pydantic `_resource` object
is pure Python and remains valid after the commit. The reload is not needed.

```python
def save(self):
    self._ensure_uow()
    self._uow.commit()
    self._loaded_in_uow = False   # stale after commit; reload on next __enter__
    self._uow = None
    return self
```

Note: the current implementation uses `commit(clear_session=False)` +
`end_session()` as two separate calls. The new implementation uses a single
`commit()` which handles session cleanup, matching the pattern in
`ProcessRunBuilder.save()`.

**Session-equivalence rationale (why this swap is safe).** There is exactly one
`LocalBackend` per client, and it holds a single `self._session`
(`local.py:106`). The UoW model is non-nestable: `backend.begin()` raises if a
session already exists (`local.py:141-144`), and `_clear_session` sets
`backend._session = None` (`local.py:117-120`). So at most one session is ever
active across the client and all its builders.

The old `save()` only kept the session open (`clear_session=False`) so that the
post-commit `get_resource(expand=True)` could run in the same session, then
closed it via `end_session()`. Since `get_resource` is being removed, nothing
runs between commit and close. `commit()` with the default `clear_session=True`
calls `_clear_session` — the exact same close that `end_session()` performed.
The two versions are therefore **session-equivalent**: same commit, same close,
no observable difference at the session level.

**Parent/child session sharing — verified non-issue.** Two distinct parent/child
mechanisms exist, and only one shares a UoW:

- `build_resource(..., parent=ResourceSchema)` (the public API path, the only one
  exercised by tests) sets `parent_resource` but leaves `parent = None`, and the
  builder is constructed with an explicit `backend`, so it owns its **own** UoW
  via `_ensure_uow()`. No UoW is shared. Closing the session in `save()` cannot
  strand any other builder, because the parent is a detached schema, not a live
  builder.
- `ResourceBuilder.add_child()` (`resource_builder.py:241-256`) is the only path
  that aliases `child._uow = parent._uow`. It is **unused and untested**
  throughout the codebase (every `add_child` call in tests targets
  `ResourceTemplateBuilder`, not `ResourceBuilder`), and child builders created
  this way are never entered or `save()`-d themselves — the parent's `__exit__`
  commits. The swap therefore has no practical blast radius here. Any latent
  alias-desync in this dead path is pre-existing (today's `end_session()` already
  closes the shared session) and is neither introduced nor worsened by this
  change.

**Safeguard — add a regression test for `ResourceBuilder.add_child()`.** This is
the one genuinely untested surface touched by the session reasoning above. If the
resource-instance `add_child()` API is intended to remain supported, add a test
in `recap/tests/` that exercises it end-to-end (parent builder + `add_child` +
clean `__exit__`), asserting the child resource is persisted and linked. If the
API is *not* intended to be supported, flag lines 241-256 as dead code for a
separate cleanup rather than leaving an unverified path live. Either way, do not
rely on the session-equivalence argument for this path without locking it in with
a test.

#### Step 4 — `_restart_uow()`: reset flag on rollback

A rollback invalidates the loaded state:

```python
def _restart_uow(self):
    if self._uow:
        self._uow.rollback()
    self._uow = self.backend.begin()
    if self.parent:
        self.parent._uow = self._uow
    self._loaded_in_uow = False   # rollback invalidates loaded state
    return self._uow
```

#### Step 5 — Remove debug `print()` statement

Remove lines 60-62:
```python
# REMOVE:
if self.parent_resource:
    print(
        f"adding child {self._resource.name} to {self.parent_resource.name}"
    )
```

If this output is useful for debugging, replace with:
```python
logger.debug(
    "Adding child resource %r to parent %r",
    self._resource.name,
    self.parent_resource.name,
)
```
and add `import logging; logger = logging.getLogger(__name__)` at the top of
the file.

---

### File 2 — `dsl/process_builder.py`

#### Step 1 — Add `_loaded_in_uow` and `_model_dirty` flags

```python
def __init__(self, ...):
    ...
    self._loaded_in_uow: bool = False
    self._model_dirty: bool = False
    ...
    try:
        self._initialize_process_run(process_run_id, campaign)
        self._loaded_in_uow = True    # mark run as fresh in this UoW
        self._steps = list(self._process_run.steps.values())
        self._resources = {}
    except Exception:
        if self._uow:
            self._uow.rollback()
            self._uow = None
        raise
```

#### Step 2 — `__enter__`: skip reload when already fresh

```python
def __enter__(self):
    self._ensure_uow()
    if getattr(self, "_process_run", None) is not None and not self._loaded_in_uow:
        # Re-entering after save() or _restart_uow() — reload to get current state
        self._process_run = self._reload_process_run(self._process_run.id)
        template = self._process_run.template
        self._process_template = self.backend.get_process_template(
            template.name, template.version, expand=True
        )
        self.name = self._process_run.name
        self.description = self._process_run.description
        self.template_name = template.name
        self.version = template.version
        self._steps = list(self._process_run.steps.values())
    return self
```

#### Step 3 — `set_params()`: remove redundant reload and persist

`backend.set_params()` already calls `session.flush()`, writing the parameter
values to the session within the open transaction. No reload or persist is
needed — `__exit__` handles the commit:

```python
def set_params(self, filled_params: type[BaseModel]):
    self._ensure_uow()
    self.backend.set_params(filled_params)
    # backend.set_params() already flushed; __exit__ will commit.
    return self
```

#### Step 4 — Track pydantic-side mutations with `_model_dirty`

`set_model()` and `assign_resource()` mutate `_process_run` as a pydantic
object. These mutations must be persisted via `update_process_run()` before
committing, otherwise they are lost. `set_params()` bypasses the pydantic
schema (it writes directly to the ORM via `backend.set_params()`), so it does
not require a `persist()` call.

```python
def set_model(self, model: ProcessRunSchema):
    if self._process_run is None:
        raise RuntimeError("ProcessRun not initialized")
    if model.id != self._process_run.id:
        raise ValueError("ID for this ProcessRun does not match the builder")
    self._process_run = model
    self._model_dirty = True          # pydantic schema mutated; persist needed

def assign_resource(
    self,
    resource_slot_name: str,
    resource: ResourceSchema,
) -> "ProcessRunBuilder":
    self._ensure_uow()
    resource_slot = None
    for slot in self._process_template.resource_slots:
        if slot.name == resource_slot_name:
            resource_slot = slot
            break
    if resource_slot is None:
        raise NoResultFound(f"Resource slot {resource_slot_name} not found")
    self._process_run = self.backend.assign_resource(
        resource_slot, resource, self._process_run
    )
    self._model_dirty = True          # ORM updated and schema refreshed; persist needed
    return self
```

#### Step 5 — `__exit__`: only persist when `_model_dirty`

```python
def __exit__(self, exc_type, exc, tb):
    if exc_type is None:
        if self._model_dirty:
            self.persist()
        self.save()
    else:
        if self._uow:
            self._uow.rollback()
        self._uow = None
```

#### Step 6 — `save()`: reset flags and commit

```python
def save(self):
    self._ensure_uow()
    self._uow.commit()
    self._loaded_in_uow = False
    self._model_dirty = False
    self._uow = None
    return self
```

#### Step 7 — `_restart_uow()`: reset flags on rollback

```python
def _restart_uow(self):
    if self._uow:
        self._uow.rollback()
    self._uow = self.backend.begin()
    self._loaded_in_uow = False
    self._model_dirty = False
    return self._uow
```

---

## Correctness invariants to verify

The following scenarios must continue to work correctly after the changes:

### Scenario A — `set_params()` only (most common, the hot path)
```python
with client.build_process_run(process_run_id=existing.id) as prb:
    p = prb.get_params("Request")
    p.queue_meta.priority = 5
    prb.set_params(p)
```
- `__init__`: loads run (SELECT) — `_loaded_in_uow = True`
- `__enter__`: skipped — `_loaded_in_uow` is True
- `set_params()`: flushes params
- `__exit__`: `_model_dirty` is False → skip `persist()` → commit

The flush from `set_params()` wrote the new priority; the commit makes it
durable. ✓

### Scenario B — `set_model()` (pydantic-side mutation)
```python
with client.build_resource(resource_id=existing.id) as rb:
    m = rb.get_model()
    m.properties.slot_info.dewar_slot_index = 3
    rb.set_model(m)
```
- `__enter__`: skipped
- `set_model()`: `_model_dirty = True`
- `__exit__`: `_model_dirty` is True → `persist()` → `update_resource()` → commit ✓

### Scenario C — `assign_resource()` on a new run
```python
with client.build_process_run(run_name, desc, template, version) as prb:
    prb.assign_resource("sample", sample_res)
    p = prb.get_params("Request")
    p.queue_meta.priority = 5
    prb.set_params(p)
```
- `__init__`: creates run (INSERT) — `_loaded_in_uow = True`
- `__enter__`: skipped
- `assign_resource()`: writes resource assignment → `_model_dirty = True`
- `set_params()`: flushes params
- `__exit__`: `_model_dirty` is True → `persist()` → `update_process_run()` → commit

Note: `persist()` will call `update_process_run(_process_run)`. At this point
`_process_run` still holds pre-`set_params()` parameter values because
`set_params()` no longer reloads. This is safe because `update_process_run()`
only updates ORM fields that differ from `_process_run.steps`, and the
`set_params()` flush already wrote the new values — SQLAlchemy's ORM dirty
tracking will not overwrite them with stale data from the pydantic schema
because the values were flushed first.

> **Potential concern**: does `update_process_run()` overwrite the flushed
> `set_params()` values with stale pydantic values?
>
> `update_process_run()` in `local.py:1241-1278` calls `av.set_value(value)`
> for each attribute using values from `_process_run.steps` (the pydantic
> schema). If `_process_run` has stale parameter values from before
> `set_params()`, `update_process_run()` would reset them — a correctness bug.
>
> **The safe handling**: when both `assign_resource()` AND `set_params()` are
> called in the same `with` block, add a reload before `persist()` in
> `__exit__`:
>
> ```python
> def __exit__(self, exc_type, exc, tb):
>     if exc_type is None:
>         if self._model_dirty:
>             # Reload to ensure pydantic schema reflects any set_params() flushes
>             self._process_run = self._reload_process_run(self._process_run.id)
>             self.persist()
>         self.save()
>     ...
> ```
>
> This adds 1 reload only when `_model_dirty` is True (i.e., when
> `set_model()` or `assign_resource()` was used alongside `set_params()`).
> For the dominant hot-path (`set_params()` only), `_model_dirty` is False,
> no reload occurs, and the fast path is preserved.

### Scenario D — Builder re-used across multiple `with` blocks
```python
builder = client.build_process_run(...)
with builder:
    prb.set_params(p1)
# After first save(), _loaded_in_uow = False
with builder:
    prb.set_params(p2)    # __enter__ reloads correctly
```
`save()` sets `_loaded_in_uow = False`, so the second `__enter__` reloads —
the original intent of `__enter__`'s reload is preserved for this pattern. ✓

### Scenario E — `on_existing="silent"` with name-based lookup (rollback path)
`_handle_existing_process_run()` calls `_restart_uow()` which resets
`_loaded_in_uow = False`. The run found there is then assigned to
`_process_run` and `_loaded_in_uow` must be set to `True`. This requires
adding `self._loaded_in_uow = True` at the end of `_handle_existing_process_run()`:
```python
def _handle_existing_process_run(self, create_error):
    self._restart_uow()
    existing = self.backend.query(...)
    ...
    self._process_run = existing[0]
    self._loaded_in_uow = True    # mark fresh after recovery load
```

---

## Expected timings after fix

Based on profiling data from `beamx`:

| Phase | Before | Expected after |
|---|---|---|
| `dewar_upsert` | 47–67 s | < 0.1 s |
| Per-request write (existing, `set_params` only) | ~0.5 s | ~0.05 s |
| 258 changed requests | 129 s | ~13 s |
| Total refresh | 212 s | ~50 s |

The remaining ~50 s is dominated by:
- `puck_prefetch` (32 s) — a `beamx`-side issue with `load="full"` on the puck
  query; see `BEAMX_SYNC_REMAINING_OPTIMISATIONS.md`
- The 258 requests that genuinely changed between refreshes (now at ~0.05 s
  each × 258 = ~13 s)

---

## Files changed

| File | Changes |
|---|---|
| `dsl/resource_builder.py` | `_loaded_in_uow` flag; `__enter__` skip guard; `save()` removes `get_resource(expand=True)`; `_restart_uow()` resets flag; `print()` removed |
| `dsl/process_builder.py` | `_loaded_in_uow` + `_model_dirty` flags; `__enter__` skip guard; `set_params()` removes reload+persist; `set_model()` + `assign_resource()` set `_model_dirty`; `__exit__` conditional persist with reload guard; `save()` + `_restart_uow()` reset flags; `_handle_existing_process_run()` sets `_loaded_in_uow` |
