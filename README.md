# RECAP (Reproducible Experiment Capture and Provenance)

<p align="center">
  <img src="docs/img/recap_logo.png" alt="RECAP logo" width="240" />
</p>

A scientific framework for reproducible experiment capture, tracking, and metadata management.

## Overview

RECAP is a Python framework that captures **experimental provenance** using a SQL database backend (via SQLAlchemy 2.0+). It provides:

- Pydantic validators
- A DSL builder API
- SQLAlchemy ORM persistence
- A unified provenance graph

## Installation

Recap is available from Pypi, install using:
```
    pip install pyrecap
```

## Getting started

As of Dec 2025, Recap clients only connect directly with sqlite databases, a REST API backend is on the roadmap.

To create and connect to a temporary sqlite database:
```python
from recap.client import RecapClient

client = RecapClient.from_sqlite() 
print(client.database_path) # print path of your database
```

if you want to generate a database at a specific location pass in the database path. You can also connect to an existing databases similarly

```python
client = RecapClient.from_sqlite("/path/to/database.db")
```

## Resources

In RECAP any trackable entity is called a **Resource**. A resource can be a physical item (samples, plates, robots), a digital item (raw detector files, processed datasets), or a logical item (intermediate computation results). Resources are treated as first-class objects: they have identity, metadata, lineage, and hierarchical structure.

### Resource Hierarchy

Resources may contain child resources. For example, a 96-well plate includes 96 wells:

```
Plate
 ├── A01
 ├── A02
 ├── ...
 └── H12
```

Each child is also a resource with its own attributes. But before we create a resource we must create a definition or a `ResourceTemplate`. This is the canonical definition of the resource to be created


### Resource Template

Example: creating a plate template with child wells:

```python
with client.build_resource_template(name="Xtal Plate",
                                    type_names=["container", "plate", "library_plate"]) as template_builder:
    for row in "ABC":
        for col in range(1, 4):
            template_builder.add_child(f"{row}{col:02d}", ["container", "well"]).close_child()
```

In the example above we create a `template_builder` that creates or modifies an existing template in the database. The `template_builder` needs a unique name and a number of tags called `type_names` which are needed to associate a `Resource` to an experiment/workflow, called `ProcessRun`. The example also shows the ability of adding child resource templates, `well` in this case.


### Properties and Property Groups

Resources can carry metadata organized into groups of related properties.

Example: We create a similar template, but this time we add properties to the plate template. The group is called `dimensions` within which we create two parameters `rows` and `columns`, we also have to specify the data `type` for the property and a `default` value. If the property has a unit, you can specify that with a `unit` key.

```python
with client.build_resource_template(name="Library Plate",
                                    type_names=["container", "plate", "library_plate"]) as template_builder:
    template_builder.add_properties({
        "dimensions": [
            {"name": "rows", "type": "int", "default": 3},
            {"name": "columns", "type": "int", "default": 4},
        ]
    })

    for row in "ABC":
        for col in range(1, 4):
            template_builder.add_child(f"{row}{col:02d}", ["container", "well"])\
             .add_properties({
                "status": [
                    {"name": "used", "type": "bool", "default": False},
                ],
                "content": [
                     {"name": "catalog_id", "type": "str", "default": ""},
                     {"name": "smiles", "type": "str", "default": ""},
                     {"name": "sequence", "type": "int", "default": col},
                     {"name": "volume", "type": "float", "default": 10.0, "unit": "uL"},
                 ]
             })\
             .close_child()
```

Visualizing ResourceTemplates, PropertyGroups and Properties

<p align="center">
  <img src="docs/img/resource_template.png" alt="Resource Template Schema" />
</p>


### Resources

Once the template is defined, you can create an instance of the Resource. When a resource is created based on a template, it automatically creates instances of the child resources as well. For example,

```python

plate = client.create_resource(name="Plate A", template_name="Library Plate")

```

will create a Resource based on the `Library Plate` template we created previously. Child resources will automatically be created mirroring the template. When these resources are created, the value of properties are set to the defaults defined in the template. 

The object returned is always a pydantic object that used as a reference/local copy, any change to the data should be made through the client. There are 2 ways to create resources. The first as shown above, simply creates the resource and returns a pydantic model. The second is with a builder where a user can specify what values to change or even add children that are not defined in the template. For example,

```python

with client.build_resource(name="Plate B", template_name="Library Plate") as resource_builder:
    resource = resource_builder.get_model()
    # Make changes to the resource and its default parameters
    resource.children["A1"].properties["status"].values["used"] = True
    # Then update the builder with the newly edited object
    resource_builder.set_model(resource) 

```

**Note**: Values in the database can _only_ be changed via builders. Simply changing the pydantic models will only change the local copy of the data. Builders are python context managers that open and clean up database connections. So if there are any validation errors that happen while modifying the database, the context manager rolls back the database transaction to a safe checkpoint

**Note**: `create_resource` and `build_resource` serve different purposes, `create_resource` simply generates the resource based on default property values and returns the corresponding pydantic model. `build_resource` creates a context manager to allow a user to modify values

You can also re-open an existing builder,

```python

with resource_builder:
    # This is the same builder as the one created above
    # Any changes here modify the resource called "Plate B"
    ...
    
```

If you have a pydantic model of a resource (either from querying the database or using `client.create_resource`) you can use it to create a builder from that,

```python

with client.build_resource(resource_id=resource.id) as rb2:
    rb2.set_model(resource)
    
```

Every time the context manager is initialized, the builder pulls the latest data for the model from the database. In the example above, the builder updates itself from the database.

## ProcessTemplates and ProcessRuns

A `ProcessTemplate` captures the execution of a workflow that manipulates resources.

- Each template contains a series of steps.
- Each step contains parameters (similar to resource properties).
- Resources are assigned into slots defined by the process template.
- ProcessRun is an instance of a ProcessTemplate
- ProcessRuns form the core provenance trail.

The figure illustrates the struceture of a ProcessTemplate:

<p align="center">
  <img src="docs/img/process_template.png" alt="Process Template Schema" />
</p>

This template consists of 3 steps. Each step is connected to the next in the order of execution using solid arrows:

- Imaging step: A container, typically plate wells are imaged under a microscope
- Echo Transfer step: The Echo 525, acoustic liquid handler is used to transfer liquid from one container to another usually from one plate's well to another plate's well
- Harvest step: Crystals from a plate are harvested and transferred into a pin which is placed in a puck

To the left of the Process Template are the input resource slots, only resources that are of type `library_plate` and `crystal_plate` can be assigned to those slots. Similarly, the right side of the template represents the output resource slot which in this case can only be of type `puck_collection`.

Resources assigned to this ProcessTemplate play different roles depending on the step. For example in step 2, `Echo Transfer`, the `library_plate` plays the role of `source` and the `crystal_plate` plays the role of `destination` respectively. Whereas in the `Harvest` step, the `crystal_plate` becomes the `source` and the `puck_collection` is the `destination`. The dotted arrows indicate the role that a `resource_slot` plays in that particular step. When a `ProcessRun` is initialized, Recap will automatically wire the assigned resources to the appropriate steps based on the template's definition.

To illustrate this, we implement the ProcessTemplate shown in the figure:

```python

with client.build_process_template("PM Workflow", "1.0") as pt:
    (
        pt.add_resource_slot("library_plate", "library_plate", Direction.input)
        .add_resource_slot("xtal_plate", "xtal_plate", Direction.input)
        .add_resource_slot("puck_collection", "puck_collection", Direction.output)
    )
    (
        pt.add_step(name="Imaging").
        .add_parameters({
             "drop": [
                {"name": "position", "type": "enum", "default": "c",
                "metadata":{"choices": {"u": {"x": 0, "y": 1}, "d": {"x": 0, "y": -1}}},
                }]
            })
            .bind_slot("plate", "xtal_plate")
            .close_step()
    )
    (
        pt.add_step(name="Echo Transfer")
        .add_parameters({
            "echo": [
                {"name": "batch", "type": "int", "default": 1},
                {"name": "volume", "type": "float", "default": 25.0, "unit": "nL"},
            ]
        })
        .bind_slot("source", "library_plate")
        .bind_slot("dest", "xtal_plate")
        .close_step()
    )
    (
        pt.add_step(name="Harvesting")
        .add_parameters({
            "harvest": [
                {"name": "arrival", "type": "datetime"},
                {"name": "departure", "type": "datetime"},
                {"name": "lsdc_name", "type": "str"},
                {"name": "harvested", "type": "bool", "default": False},
            ]
        })
        .bind_slot("source", "xtal_plate")
        .bind_slot("dest", "puck_collection")
        .close_step()
    )

```

We've already created a template of a library plate above, now we create the template for the crystal plate, puck collection, puck and pin:

```python

# Crystal plate template
with client.build_resource_template(name="Crystal Plate",
                                    type_names=["container", "plate", "xtal_plate"]) as template_builder:
    template_builder.add_properties({
        "dimensions": [
            {"name": "rows", "type": "int", "default": 3},
            {"name": "columns", "type": "int", "default": 4},
        ]
    })

    for row in "ABC":
        for col in range(1, 4):
            template_builder.add_child(f"{row}{col:02d}", ["container", "well"])\
             .add_properties({
                "well_map": [
                     {"name": "well_pos_x", "type": "int", "default": 0},
                     {"name": "well_pos_y", "type": "int", "default": 0},
                     {"name": "echo", "type": "str", "default": ""},
                     {"name": "shifter", "type": "str", "default": ""},
                 ]
             })\
             .close_child()

# Puck collecton template
with client.build_resource_template(
    name="Puck Collection", type_names=["container", "puck_collection"]
) as pc:
    pass

# Puck template
with client.build_resource_template(
    name="Puck", type_names=["container", "puck"]
) as pkb:
    pkb.add_properties({
        "details": [
             {"name": "type", "type": "str", "default": "unipuck"},
             {"name": "capacity", "type": "int", "default": 16},
        ]
    })
    puck_template = pkb.get_model()

# Pin template
with client.build_resource_template(
    name="Pin", type_names=["container", "pin"]
) as pin:
    pin.add_properties(
        "mount": [
            {"name": "position", "type": "int", "default": 0},
            {"name": "sample_name", "type": "str", "default": ""},
            {"name": "departure", "type": "datetime", "default": None},
        ]
    )
```

**Note**: For a given database, it is only required to define a template _once_. Templates are reusable definitions of a resource or process.

Before we initialize instances of these containers or create a process run, we must associate the current session with a `Campaign`.

A **Campaign** stores the scientific context:

- Proposal identifiers
- SAF/regulatory details
- Arbitrary metadata
- All `ProcessRun` objects belonging to the project

```
Campaign
  └── ProcessRun
         ├── Step 1
         ├── Step 2
         └── Step 3
```

Creating a campaign:

```python
campaign = client.create_campaign(
    name="Experiment visit on 12/12/25",
    proposal_id="399999",
    saf_id="123"
)
```

## Instantiating a ProcessRun

Any ProcessRun or Resource created after setting or creating a campaign, is automatically associated with that campaign. Running the next snippet of code that creates a ProcessRun, will add it to the campaign we created. Recap will raise an exception if a campaign is not set.

```python

test_xtal_plate = client.create_resource(name="Test crystal plate", template_name="Crystal Plate", version="1.0")

test_library_plate = client.create_resource(name="Test library plate", template_name="Library Plate", version="1.0")

test_puck_collection = client.create_resource("Test puck collection", "Puck Collection")

with client.build_process_run(
    name="Run 001",
    description="Fragment screening test run",
    template_name="PM Workflow",
    version="1.0"
) as prb:
    prb.assign_resource("library_plate", test_library_plate)
    prb.assign_resource("xtal_plate", test_xtal_plate)
    prb.assign_resource("puck_collection", test_puck_collection)
    process_run = prb.get_model()
```

The figure below shows a visual representation of the resources created. Resources are assigned to the appropriate slot which get wired to the steps they belong to

<p align="center">
  <img src="docs/img/process_run.png" alt="Process Template Schema" />
</p>


## Adding child resources and steps during runtime

There are cases when templates cannot capture child resources and step resources ahead of time. For example, a puck collection may have an arbitrary number of pucks. To assign child resources during runtime, we can use the `add_child` method in the resource builder

```python

with client.build_resource(resource_id=test_puck_collection.id) as pcb:
    pcb.add_child(name="Puck01", template_name="Puck", template_version="1.0")
    
```

Or if you have a reference to the template id:

```python

with client.build_resource(resource_id=test_puck_collection.id) as pcb:
    pcb.add_child(name="Puck01", template_id=puck_template.id)

```

Child steps can be added dynamically in cases where details are unknown ahead of time. For example, to capture an Echo Transfer step from 1 well of the library plate to the crystal plate we can do the following

```python

with client.build_process(process_id=process_run.id) as prb:
    # Generate a pydantic model for the child step
    echo_transfer_step = process_run.steps["Echo Transfer"].generate_child()
    # Update its values
    echo_transfer_step.parameters.echo.values.batch = 2
    echo_transfer_step.parameters.echo.values.volume = 20
    echo_transfer_step.resources["source"] = test_library_plate.children["A1"]
    echo_transfer_step.resources["dest"] = test_xtal_plate.children["A1a"]
    # Add it to the database
    prb.add_child_step(echo_transfer_step)

```

## Querying Data

RECAP exposes a small Query DSL on top of the configured backend (SQLAlchemy or another adapter) so that you can express provenance-oriented queries in a fluent, chainable style.

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
```

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

### Eager Loading Related Data with `include`

Queries can preload related entities via the `include` helper. Each `include` translates to a string path that the backend understands (e.g., for SQLAlchemy that might become `joinedload` or `selectinload`). The type-specific queries expose more ergonomic methods:

- `CampaignQuery.include_process_runs()`
- `ProcessRunQuery.include_steps(include_parameters: bool = False)`
- `ProcessRunQuery.include_resources()`
- `ResourceQuery.include_template()`

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

### Implementation Note

Internally, each query constructs a `QuerySpec`:

```python
class QuerySpec(BaseModel):
    filters: dict[str, Any] = {}
    predicates: Sequence[Any] = ()
    orderings: Sequence[Any] = ()
    preloads: Sequence[str] = ()
    limit: int | None = None
    offset: int | None = None
```

Your backend adapter receives the model type and `QuerySpec` and is responsible for translating that into SQLAlchemy queries or other data sources. This separation keeps the public Query DSL stable even if the backend implementation changes.

## Roadmap

- REST API backend
- Web UI for campaign/process management
