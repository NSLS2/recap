# RECAP tests

Tests are grouped by the feature they exercise, with REST contracts identifiable
by their `test_rest_*` names rather than a separate transport-layer suite.

- `resources/`: resources, templates, attribute values and shared domain invariants.
- `processes/`: process templates/runs, assignments and runtime child steps.
- `namespaces/`: namespace visibility, ownership, discovery, policies and migrations.
- `query/`: field predicates, loading, flat batches and canonical materialization.
- `client/`: client/view lifetime, common builder contracts, transport and cross-feature workflows.
- `security/`: authentication, authorization compilation/snapshots and request security.
- `server/`: server/database configuration and command/request codecs.
- `lifecycle/`: lifecycle transitions, mutable editing, revisions, audit and idempotency.
- `perf/`: every performance-marked case, including cases split from mixed files.
- `helpers/`: shared fixtures, fake adapters, transport factories and SQL profiling.

Root `conftest.py` retains fixture reachability, isolated premigrated database
copies and `count_statements`/`statement_counter`. Mixed-file performance cases
import their existing helper/fixture definitions from the feature module; they
do not duplicate fixture setup or assertions. Authorization test data moves
with the security tests so their existing relative fixture paths remain valid.

Use the unchanged tasks:

```bash
pixi run -e dev test
pixi run -e dev test-integration
pixi run -e dev test-performance
pixi run -e dev test-all
```

The pruning audit maps every original function to its retained, merged or
removed disposition and final location under `docs/superpowers/plans/`.
