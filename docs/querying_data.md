# Querying Data

RECAP exposes a small Query DSL on top of the configured backend (SQLAlchemy or another adapter) so that you can express provenance-oriented queries in a fluent, chainable style. Query objects are immutable; each chain returns a new query with your filters/preloads applied.

The query builder lives on the client as `client.query_maker()` and exposes type-specific entry points:

- `campaigns()` -> `CampaignQuery`
- `process_templates()` -> `ProcessTemplateQuery`
- `process_runs()` -> `ProcessRunQuery`
- `resources()` -> `ResourceQuery`
- `resource_templates()` -> `ResourceTemplateQuery`

Under the hood, these all use a common `BaseQuery` and a backend-provided `.query(model, spec)` implementation. The `QuerySpec` object carries filters, predicates, ordering, preloads, and pagination options down to the backend. Query objects are immutable: every operation like `filter` or `include` returns a new query instance.

### Getting a QueryDSL Handle

Assuming you have a configured client:

```python
qm = client.query_maker()

# Query entry points
campaigns = qm.campaigns()
runs = qm.process_runs()
resources = qm.resources()
templates = qm.resource_templates()
process_templates = qm.process_templates()
```

> Note: when using `RecapClient`, if a campaign is set via `create_campaign()` or
> `set_campaign()`, resource and process run queries are scoped to that campaign
> by default. You can override per-call by passing `campaign=` to `resources()` /
> `process_runs()`, or leave it unset to query across campaigns.

### Basic Filtering

The simplest way to filter is with `filter(**kwargs)`, which translates into backend-specific filter expressions.

List all campaigns with a given proposal id:

```python
campaigns = (
    client.query_maker()
    .campaigns()
    .filter(proposal="399999")
    .all()
)

for c in campaigns:
    print(c.id, c.name)
```

Fetch a single campaign by name (or `None` if not found):

```python
campaign = (
    client.query_maker()
    .campaigns()
    .filter(name="Beamline Proposal 4321")
    .first()
)

if campaign is None:
    raise RuntimeError("No such campaign")
```

Counting results:

```python
n_runs = (
    client.query_maker()
    .process_runs()
    .count()
)
print("Total runs:", n_runs)
```

### Filtering Resource Templates by Type

`ResourceTemplateQuery` adds a convenience helper `filter_by_types` for semantic resource types:

```python
xtal_plate_templates = (
    client.query_maker()
    .resource_templates()
    .filter_by_types(["xtal_plate"])
    .all()
)

for tmpl in xtal_plate_templates:
    print(tmpl.name, tmpl.types)
```

This corresponds directly to the examples in the workflow section where we create templates tagged with types like `["container", "xtal_plate", "plate"]` or `["library_plate"]`.

### Filtering Resources by Properties

`ResourceQuery.filter_property` lets you compare against typed property values (int/float/bool/str/datetime inferred from your input). The property group is optional; pass it when you need to disambiguate:

```python
plates = (
    client.query_maker()
    .resources()
    .filter_property("rows", gt=100, group="dimensions")
    .all()
)
```

Scope a property filter to the descendants of a parent resource with `under_parent`:

```python
child_hits = (
    client.query_maker()
    .resources()
    .filter_property("height", gt=10)
    .under_parent(parent_resource)
    .all()
)
```

### Filtering Process Runs by Step Parameters

`ProcessRunQuery.filter_parameter` works like `filter_property` but targets step parameters. You can optionally narrow by step name and parameter group name; otherwise the match applies to any step/group:

```python
runs = (
    client.query_maker()
    .process_runs()
    .filter_parameter("dwell", gt=10, group="Exposure", step="Collect")
    .all()
)
```

### Eager Loading Related Data with `include`

Queries can preload related entities via the `include` helper. Each `include` translates to a string path that the backend understands (e.g., for SQLAlchemy that might become `joinedload` or `selectinload`). The type-specific queries expose more ergonomic methods:

- `CampaignQuery.include_process_runs()`
- `ProcessRunQuery.include_steps(include_parameters: bool = False)`
- `ProcessRunQuery.include_resources()`
- `ProcessTemplateQuery.include_step_templates()`
- `ProcessTemplateQuery.include_resource_slots()`
- `ResourceQuery.include_template()`
- `ResourceTemplateQuery.include_children()`
- `ResourceTemplateQuery.include_attribute_groups()`
- `ResourceTemplateQuery.include_types()`

Example: load campaigns and their process runs in one go:

```python
campaigns = (
    client.query_maker()
    .campaigns()
    .include_process_runs()
    .all()
)

for c in campaigns:
    print("Campaign:", c.name)
    for run in c.process_runs:
        print("  Run:", run.name)
```

#### Example: load runs with steps and parameter groups

```python
runs = (
    client.query_maker()
    .process_runs()
    .include_steps(include_parameters=True)
    .all()
)

# Fetch process templates with their steps and resource slots
pt = (
    client.query_maker()
    .process_templates()
    .filter(name="Workflow-1")
    .include_step_templates()
    .include_resource_slots()
    .first()
)

# Fetch resource templates with children, attr groups, and types
rt = (
    client.query_maker()
    .resource_templates()
    .filter(name="Plate")
    .include_children()
    .include_attribute_groups()
    .include_types()
    .first()
)

for run in runs:
    print(f"Run: {run.name}")
    for step_num, step in enumerate(run.steps):
        print(f"\tStep {step_num}: {step.name}")
        for pg_num, (param_group_name, param_group) in enumerate(step.parameters.items()):
            print(f"\t\tGroup {pg_num}: {param_group_name}")
            for param_name, param_value in param_group.values.items():
                print(f"\t\t\t{param_name} : {param_value}")
```

#### Example: load resources with their template

```python
library_plates = (
    client.query_maker()
    .resources()
    .filter(types__names_in=["library_plate"])
    .include_template()
    .all()
)

for plate in library_plates:
    print("Resource:", plate.name)
    print("  Template:", plate.template.name)
```

### Provenance Queries

#### "Which campaigns touched this sample?"

```python
sample = (
    client.query_maker()
    .resources()
    .filter(name="Sample 42")
    .first()
)

if sample is None:
    raise RuntimeError("Sample not found")

runs = (
    client.query_maker()
    .process_runs()
    .filter(resources__id=sample.id)
    .include_steps()
    .all()
)

campaign_ids = {run.campaign_id for run in runs}
campaigns = (
    client.query_maker()
    .campaigns()
    .filter(id__in=list(campaign_ids))
    .all()
)

for c in campaigns:
    print("Campaign:", c.name)
```

#### "Show me a full tree for a campaign"

```python
campaign = (
    client.query_maker()
    .campaigns()
    .filter(name="Buffer Prep")
    .include_process_runs()
    .first()
)

if campaign is None:
    raise RuntimeError("No such campaign")

runs = (
    client.query_maker()
    .process_runs()
    .filter(campaign_id=campaign.id)
    .include_steps(include_parameters=True)
    .include_resources()
    .all()
)

for run in runs:
    print("Run:", run.name)
    print("  Resources:")
    for assignment in run.resources:
        print("   -", assignment.resource.name, f"({assignment.role})")

    print("  Steps:")
    for step in run.steps:
        print("   -", step.name)
        for group in step.parameters:
            for attr in group.values:
                print(f"       {group.group_name}.{attr.name} = {attr.value}")
```

### Pagination and Ordering

All query types expose generic helpers:

- `where(*predicates)`
- `order_by(*orderings)`
- `limit(value)`
- `offset(value)`

The exact predicate and ordering objects are backend-specific, but the chaining API is stable.

Example: fetch the 10 most recent runs:

```python
from recap.db.models import ProcessRun  # or use backend-specific fields

recent_runs = (
    client.query_maker()
    .process_runs()
    .order_by(ProcessRun.created_at.desc())
    .limit(10)
    .all()
)

for run in recent_runs:
    print(run.created_at, run.name)
```
