======================================================
RECAP (Reproducible Experiment Capture and Provenance)
======================================================

.. image:: https://github.com/NSLS2/recap/actions/workflows/testing.yml/badge.svg
   :target: https://github.com/NSLS2/recap/actions/workflows/testing.yml

.. image:: docs/source/_static/recap_logo.png
   :alt: Project Logo
   :width: 220

A scientific framework for Reproducible Experiment Capture, tracking, and metadata management

* Free software: 3-clause BSD license
* Documentation: In progress...

RECAP API Quickstart
--------------------

**Audience:** Experimental scientists and engineers who want to define, run, and track repeatable lab or data‑processing workflows using RECAP.

**Goal:** Start with zero context and walk through creating:

1. a **Process Template** (the recipe),
2. **Resource Templates** (the things your process uses or produces), then
3. a **Process Run** (an actual instance with parameters filled in).

This document explains each step and annotates a complete, runnable example.

Core Concepts
-------------

* **ProcessTemplate**: A reusable recipe for an experiment or operation (e.g., "Liquid transfer, then heat").
* **StepTemplate**: One step in that recipe (e.g., `Transfer`, `Heat plate`). Steps can define **parameter groups** (like `volume_transfer` or `heat_to`) containing attributes such as `volume` or `temperature`.
* **Resource Types & Resource Templates**: Describe allowable inputs/outputs (e.g., a *96‑well plate* with rows/columns and per‑well attributes). Resources are typed (e.g., `container`, `plate`, `operator`) to control compatibility.
* **Resource Slots**: Named inputs/outputs required by a ProcessTemplate (e.g., `Input plate 1` of type `container`). In a run, you **assign** actual resources to these slots.
* **ProcessRun**: A concrete execution of a ProcessTemplate. You assign resources to slots and fill in step parameters.

Deep dive: Resource Slots — why they exist
------------------------------------------

Resource slots define the **interface** between a process template and the real world. They’re required for:

- **Decoupling recipe from inventory (late binding).** Templates stay reusable because they don’t hard‑code specific plates/files/instruments. At run time, you *assign* whatever concrete resource matches the slot’s type (e.g., any ``container`` with a compatible template).
- **Type‑safe compatibility.** Each slot declares allowed **resource type tags** (e.g., ``container``, ``operator``, ``file``). Assignment is validated so a ``pipette`` can’t be plugged into a ``plate`` slot. This prevents subtle lab/runtime errors.
- **Role binding inside steps.** Steps refer to resources by **role** (``source``, ``dest``, ``operator``) rather than instance names. A slot is bound to a role (via ``.bind(...)``), so your step logic is stable while concrete resources vary per run.
- **Provenance & audit.** The run records exactly *which* resource filled each slot (e.g., which 96‑well plate and which operator). This makes results reproducible and traceable.
- **Portability and versioning.** Slots act like a stable **contract** for the template. You can upgrade internals while preserving slot names/types so downstream tooling and campaigns don’t break.
- **Workflow composition.** Slots make **I/O explicit**. Output slots can produce resources that feed into downstream processes in a **Campaign**, giving you a clear DAG of resources and processes.
- **Automation & scheduling.** Robots/LIMS can reserve and stage resources to satisfy slots before execution (e.g., ensure the specified ``operator`` and correct ``container`` are available).

**Common patterns**

- **Input vs Output:** Use ``Direction.input`` for required inputs; mirror with ``Direction.output`` for produced artifacts.
- **Single vs multiple:** A template can model one‑to‑one or one‑to‑many by defining distinct slots (e.g., ``Input plate 1``, ``Input plate 2``) or a collection pattern if supported.
- **Human‑readable names:** Make slot names descriptive (``Destination plate``, ``Transfer operator``). These appear in UIs and logs.

**Anti‑patterns**

- Binding steps directly to concrete resource *names* instead of slots. This couples a template to a specific inventory item and kills reuse.

**Mini example (with an output)**

.. code-block:: python

   with client.process_template("QC Measure", "1.0.0") as ed:
       (
           ed.add_resource_slot("Sample plate", "container", Direction.input)
             .add_resource_slot("Detector", "instrument", Direction.input)
             .add_resource_slot("QC report", "file", Direction.output)
             .add_step("Measure").bind("Sample plate", "target").bind("Detector", "reader")
             .param_group("acquisition").add_attribute("exposure", "float", "s", 0.5)
             .close_group().close_step()
       )

.. tip::

   **Mental model:** **Template** = blueprint, **Run** = a specific experiment. **Resource Template** = template for a physical/virtual thing, **Resource** = the actual thing.


Prerequisites
-------------

- Python 3.10+
- SQLAlchemy 2.0+
- Pydantic 2.0+
- A configured database session (``db_session``) and a working RECAP install.


End‑to‑End Example (annotated)
------------------------------

1) Create a Process Template
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

We’ll define a simple two‑step process:

1. Transfer liquid from one plate to another using an operator.
2. Heat the destination plate.

.. code-block:: python

   from itertools import product

   from recap.client.base_client import RecapClient
   from recap.db.process import Direction

   client = RecapClient(session=db_session)

   # Create or update a Process Template named "Test" version "0.0.1".
   with client.process_template("Test", "0.0.1") as ed:
       # Define required input resource slots for the process (typed and named).
       (
           ed.add_resource_slot(
               "Input plate 1",            # a human‑readable slot name
               "container",                # required resource type
               Direction.input,             # input vs. output
               create_resource_type=True    # auto‑create the resource type tag if missing
           )
           .add_resource_slot("Input plate 2", "container", Direction.input)
           .add_resource_slot(
               "Liquid transfer operator", "operator", Direction.input,
               create_resource_type=True
           )

           # Step 1: Transfer
           .add_step("Transfer")
               .bind("Input plate 1", "source")     # role binding inside the step
               .bind("Input plate 2", "dest")
               .bind("Liquid transfer operator", "operator")
               .param_group("volume transfer")        # group logical parameters
                   .add_attribute(
                       attr_name="volume", value_type="float", unit="uL", default=0.0
                   )
                   .add_attribute(
                       attr_name="rate", value_type="float", unit="uL/sec", default=0.0
                   )
               .close_group()
           .close_step()

           # Step 2: Heat
           .add_step("Heat plate")
               .bind("Input plate 2", "target")
               .param_group("heat to")
                   .add_attribute("temperature", "float", "degC", "0.0")
               .close_group()
           .close_step()
       )

**What happened here?**

- You created a process blueprint ``Test:0.0.1`` with three **input slots** and two **steps**.
- Each step binds the slots to roles that the step expects (e.g., ``source``, ``dest``, ``operator``).
- Each step defines a **parameter group** with typed attributes and optional defaults.

.. tip::

   Choose stable **template names** and **versions**. Changing versions lets you evolve protocols while preserving historical runs.


2) Create Resource Templates
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

We’ll make two resource templates: a **96‑well plate** (with per‑well metadata) and a simpler **sample holder**.

.. code-block:: python

   # 96‑well plate template: global properties + per‑well children
   with client.resource_template("96 well plate", ["container", "plate"]) as rt:
       rt.prop_group("dimensions") \
         .add_attribute("rows", "float", "", 8) \
         .add_attribute("columns", "float", "", 12)

       well_cols = "ABCDEFGH"
       well_rows = [i for i in range(1, 13)]
       well_names = [f"{wn[0]}{wn[1]}" for wn in product(well_cols, well_rows)]

       for well_name in well_names:
           (
               rt.add_child(well_name, ["container", "well"])    # define a child resource
                 .prop_group(group_name="well_data")
                   .add_attribute("sample_name", "str", "", "")
                   .add_attribute("buffer_name", "str", "", "")
                   .add_attribute("volume", "int", "uL", "0")
                   .add_attribute("mixing", "str", "", "")
                   .add_attribute("stock", "bool", "", "False")
                   .add_attribute("notes", "str", "", "")
                 .close_group()
               .close_child()
           )

   # Sample holder template: 2×9 with simpler per‑well metadata
   with client.resource_template("sample holder", ["container", "plate"]) as rt:
       rt.prop_group("dimensions") \
         .add_attribute("rows", "int", "", 2) \
         .add_attribute("columns", "int", "", 9)

       for well_num in range(1, 19):
           (
               rt.add_child(str(well_num), ["container", "well"]) \
                 .prop_group("sample_holder_well_data") \
                   .add_attribute("sample_name", "str", "", "") \
                   .add_attribute("buffer_name", "str", "", "") \
                   .add_attribute("volume", "float", "uL", "0") \
                 .close_group() \
               .close_child()
           )

**What happened here?**

- You registered two **resource templates** with types (``container``, ``plate``, ``well``).
- The 96‑well plate defines **children** for each well and groups per‑well metadata under ``well_data``.
- These templates control what attributes exist when you later create actual resources in a run.

.. tip::

   Use **tags** (like ``container``, ``well``, ``operator``) consistently. Your process steps will bind to these resource types, preventing invalid assignments.


3) Instantiate a Process Run and Fill Parameters

Now create an actual run from the `Test:0.0.1` template, instantiate resources, assign them to the process slots, and set step parameters.

.. code-block:: python

    with client.process\_run(name="test\_run", template\_name="Test", version="0.0.1") as run:
        \# Create actual resources from templates (these become assignable to slots)
        run.create\_resource("96 well plate", "96 well plate")
        run.create\_resource("Test destination plate", "sample holder")


           # Assign resources to the process’s declared input slots
           run.assign_resource("Input plate 1", resource_name="96 well plate") \
              .assign_resource("Input plate 2", resource_name="Test destination plate")

           # Read, edit, and persist step parameters
           transfer_params = run.get_params("Transfer")             # returns a typed object
           transfer_params.volume_transfer.volume = 50
           transfer_params.volume_transfer.rate = 1
           print(transfer_params)                                    # inspect before saving
           run.set_params(transfer_params)                           # write back to the run

           heat_params = run.get_params("Heat plate")
           heat_params.heat_to.temperature = 100
           run.set_params(heat_params)


**About parameters: typed Pydantic models & validation**

``get_params(step_name)`` returns a **Pydantic model** that mirrors the template’s parameter groups and attributes. You can inspect its schema and fill fields with proper Python types. Calling ``set_params(model)`` will **validate** and persist the data for that step.

.. code-block:: python

   from pydantic import ValidationError

   # Inspect the model returned by get_params
   params = run.get_params("Transfer")
   print(params.model_dump())               # current values (defaults + any edits)
   print(params.model_json_schema())        # full JSON schema (types, required, etc.)

   # Fill with correct types
   params.volume_transfer.volume = 50.0     # float
   params.volume_transfer.rate = 1.0        # float

   # Persist with validation
   try:
       run.set_params(params)
   except ValidationError as e:
       # If types/constraints don't match the template, you'll see a detailed error
       print("Parameter validation failed:", e)

*Key points*

- The parameter object is **strongly typed** (via Pydantic) per your template.
- ``set_params(...)`` runs **validation**; if values don’t conform (e.g., wrong type/unit/required missing), a ``ValidationError`` is raised.
- Use ``.model_dump()`` for current state and ``.model_json_schema()`` to programmatically discover structure and constraints.

**What happened here?**

- ``process_run(...)`` created a **ProcessRun** linked to your template.
- ``create_resource(name, template)`` materialized actual resources from your **resource templates**.
- ``assign_resource(slot, resource_name=...)`` bound those resources to the template’s input **slots**.
- ``get_params(step)`` returned a typed parameter model for that step; you edited values and wrote them back with ``set_params(...)``.
- Exiting the ``with`` block commits the run and all related objects to the database.

.. tip::

   If you see validation errors, check that your **value types** (``int``, ``float``, ``str``, ``bool``) and **units** match what the template expects, and that required assignments (slots) are complete.


How This Relates to RECAP’s Data Flow
-------------------------------------

::

   RECAP DSL Builders (your code above)
           ↓
   SQLAlchemy ORM Models
           ↓
   Database (auditable templates & runs)

- You can feed RECAP with YAML/JSON to parameterize templates and runs; Pydantic validates shapes and types.
- The builder DSL (what you used) gives a readable, chainable Python interface for scientists.


Troubleshooting & FAQs
----------------------

**Q: My resource won’t assign to a slot. Why?**
  A: Check the slot’s required **type** (e.g., ``container``) and your resource’s tags. They must be compatible.

**Q: ``get_params`` returns a model, but ``set_params`` fails.**
  A: Ensure you didn’t remove required groups/attributes, and that values conform to the declared ``value_type``.

**Q: How do I version changes safely?**
  A: Bump the **template version** (e.g., ``0.0.2``) when you change step structure, slots, or parameter schemas. Old runs keep their original version.

**Q: Where is commit handled?**
  A: The context managers (``with ...``) manage lifecycle and commit on successful exit. Exceptions inside the block will roll back.


Best Practices
--------------

- **Name things for humans.** Slots like ``Input plate 1`` are clearer than ``ip1``.
- **Group parameters logically.** Keep related attributes together (``volume_transfer``, ``heat_to``).
- **Prefer templates first.** Define resource and process templates before runs to maximize reuse.
- **Tag consistently.** Use a stable set of resource type tags (``container``, ``well``, ``operator``, etc.).
- **Use defaults thoughtfully.** Defaults enable quick prototyping; record actual values in runs for provenance.


Next Steps
----------

- Add outputs by declaring **output** slots in your ProcessTemplate (mirrors ``Direction.input``).
- Chain processes into a **Campaign** to capture multi‑step experimental programs.
- Drive runs from **YAML/JSON** to reduce boilerplate in Python notebooks.

*You’re ready to build richer templates and automate more of your lab workflow with RECAP.*



