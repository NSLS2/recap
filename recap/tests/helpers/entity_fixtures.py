"""Attached ORM graphs shared by collection, materialization, and transport tests."""

from contextlib import nullcontext
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.db.namespace import Namespace
from recap.db.process import (
    ProcessRun,
    ProcessTemplate,
    ResourceAssignment,
    ResourceSlot,
)
from recap.db.resource import Resource, ResourceTemplate, ResourceType
from recap.db.step import Step, StepTemplate, StepTemplateResourceSlotBinding
from recap.utils.general import Direction


def _seed_resource_graph(db_session):
    namespace = Namespace(path=f"entities/{uuid4().hex}", metadata_json={})
    resource_type = ResourceType(name=f"entity-type-{uuid4().hex}")
    parent_template = ResourceTemplate(
        name=f"ParentTemplate-{uuid4().hex}", namespace=namespace
    )
    parent_template.types.append(resource_type)
    property_group = AttributeGroupTemplate(name="Measurements")
    property_group.attribute_templates.append(
        AttributeTemplate(name="dose", value_type="int", default_value="1")
    )
    parent_template.attribute_group_templates.append(property_group)
    child_template = ResourceTemplate(
        name="child", namespace=namespace, parent=parent_template
    )
    parent = Resource(name="parent", template=parent_template, namespace=namespace)
    child = parent.children["child"]
    db_session.add_all(
        [namespace, resource_type, parent_template, child_template, parent]
    )
    db_session.flush()
    return parent, child, parent_template, child_template


def _seed_process_template(db_session):
    namespace = Namespace(path=f"entities/process-{uuid4().hex}", metadata_json={})
    process_template = ProcessTemplate(
        name=f"Process-{uuid4().hex}", version="1.0", namespace=namespace
    )
    step_template = StepTemplate(name="Collect", process_template=process_template)
    parameter_group = AttributeGroupTemplate(name="Exposure")
    parameter_group.attribute_templates.append(
        AttributeTemplate(name="dwell", value_type="int", default_value="1")
    )
    step_template.attribute_group_templates.append(parameter_group)
    db_session.add_all([namespace, process_template, step_template])
    db_session.flush()
    return process_template, step_template


@dataclass(frozen=True)
class LoadedPlateGraph:
    """Detached identities for independently committed cold-session measurements."""

    namespace_path: str
    plate_id: UUID
    well_ids: tuple[UUID, ...]
    source_ids: tuple[UUID, UUID]
    run_id: UUID


def seed_loaded_well_plate(session: Session, well_count: int) -> LoadedPlateGraph:
    """Seed distinct well definitions and two transfers; caller owns the transaction."""
    if well_count not in {12, 96}:
        raise ValueError("well_count must be 12 or 96")
    namespace = Namespace(path=f"bulk-loading/{uuid4().hex}", metadata_json={})
    container_type = ResourceType(name=f"container-{uuid4().hex}")
    sample_type = ResourceType(name=f"sample-{uuid4().hex}")
    plate_template = ResourceTemplate(name="Loaded plate", namespace=namespace)
    plate_template.types.append(container_type)
    for index in range(well_count):
        template = ResourceTemplate(
            name=f"{chr(65 + index // 12)}{index % 12 + 1:02d}",
            namespace=namespace,
            parent=plate_template,
        )
        template.types.append(sample_type)
        for name, attributes in (
            ("Measurements", [("Mass", "float", "mg"), ("Readings", "array", None)]),
            ("Identity", [("Note", "str", None), ("Label", "str", None)]),
            ("Loading", [("Volume", "float", "uL")]),
        ):
            group = AttributeGroupTemplate(name=name, resource_template=template)
            for attribute_name, value_type, unit in attributes:
                group.attribute_templates.append(
                    AttributeTemplate(
                        name=attribute_name, value_type=value_type, unit=unit
                    )
                )
    source_template = ResourceTemplate(name="Source sample", namespace=namespace)
    source_template.types.append(sample_type)
    group_template = ResourceTemplate(name="Received container", namespace=namespace)
    group_template.types.append(container_type)
    session.add_all(
        [
            namespace,
            container_type,
            sample_type,
            plate_template,
            source_template,
            group_template,
        ]
    )
    # Initialization uses template IDs to detect recursion; assign them before resources.
    session.flush()
    group = Resource(
        name="Received samples", template=group_template, namespace=namespace
    )
    sources = tuple(
        Resource(
            name=f"Source {index}",
            template=source_template,
            parent=group,
            namespace=namespace,
        )
        for index in range(2)
    )
    plate = Resource(name="Loaded plate", template=plate_template, namespace=namespace)
    for well in plate.children.values():
        well.properties["Measurements"].values["Mass"] = 2.5
        well.properties["Measurements"].values["Readings"] = [1, 1, 2]
        well.properties["Identity"].values["Label"] = "sample"
        well.properties["Loading"].values["Volume"] = 10.0
    session.add_all([group, *sources, plate])
    session.flush()
    process = ProcessTemplate(
        name="Manual Transfer", version="1.0", namespace=namespace
    )
    transfer = StepTemplate(name="Transfer", process_template=process)
    params = AttributeGroupTemplate(name="Transfer details", step_template=transfer)
    params.attribute_templates.extend(
        [
            AttributeTemplate(
                name="Operator", value_type="str", default_value="synthetic"
            ),
            AttributeTemplate(
                name="Timestamp", value_type="str", default_value="2026-10-23T00:00:00Z"
            ),
            AttributeTemplate(
                name="Volume", value_type="float", unit="uL", default_value="10"
            ),
        ]
    )
    slots = {}
    for name, resource_type, direction in (
        ("group", container_type, Direction.input),
        ("plate", container_type, Direction.output),
        ("source", sample_type, Direction.input),
        ("destination", sample_type, Direction.output),
    ):
        slot = ResourceSlot(
            name=name,
            process_template=process,
            resource_type=resource_type,
            direction=direction,
            required=name in {"group", "plate"},
        )
        slots[name] = slot
        StepTemplateResourceSlotBinding(
            role=name, step_template=transfer, resource_slot=slot
        )
    session.add(process)
    session.flush()
    run = ProcessRun(
        name="Manual transfer",
        description="Two transfers",
        template=process,
        namespace=namespace,
    )
    session.add(run)
    session.flush()
    run.resources[slots["group"]] = group
    run.resources[slots["plate"]] = plate
    root = run.steps["Transfer"]
    wells = tuple(plate.children.values())
    for index, source in enumerate(sources):
        child = Step(name=f"Transfer {index}", template=transfer, parent=root)
        run.steps[child.name] = child
        session.flush()
        for name, resource in (("source", source), ("destination", wells[index])):
            child.assignments[slots[name].id] = ResourceAssignment(
                resource_slot_id=slots[name].id,
                resource_slot=slots[name],
                process_run=run,
                step=child,
                resource=resource,
            )
    session.flush()
    return LoadedPlateGraph(
        namespace_path=namespace.path,
        plate_id=plate.id,
        well_ids=tuple(well.id for well in wells),
        source_ids=(sources[0].id, sources[1].id),
        run_id=run.id,
    )


@dataclass(frozen=True)
class PlateWorkflowInputs:
    group_id: UUID
    source_ids: tuple[UUID, UUID]
    well_names: tuple[str, ...]


def setup_plate_workflow(client, well_count):
    """Create definitions and source assets through the shared public builders."""
    if well_count not in {12, 96}:
        raise ValueError("well_count must be 12 or 96")
    well_names = tuple(
        f"{chr(65 + index // 12)}{index % 12 + 1:02d}" for index in range(well_count)
    )
    measurements = [
        {"name": "Mass", "type": "float", "unit": "mg", "default": None},
        {"name": "Readings", "type": "array", "default": [1, 1, 2]},
    ]
    with client.build_resource_template(
        name="Loaded plate", type_names=["plate"]
    ) as template:
        for name in well_names:
            template.add_child(name, ["well"]).add_properties(
                {
                    "Measurements": measurements,
                    "Identity": [
                        {"name": "Note", "type": "str", "default": None},
                        {"name": "Label", "type": "str", "default": None},
                    ],
                    "Loading": [
                        {
                            "name": "Volume",
                            "type": "float",
                            "unit": "uL",
                            "default": None,
                        }
                    ],
                }
            ).close_child()
        template.finalize()
    with client.build_resource_template(
        name="Received container", type_names=["container"]
    ) as template:
        template.add_properties(
            {
                "Receipt": [
                    {"name": "Batch", "type": "str", "default": "synthetic-batch"},
                    {
                        "name": "Operator",
                        "type": "str",
                        "default": "synthetic-operator",
                    },
                ]
            }
        )
        template.finalize()
    with client.build_resource_template(
        name="Source sample", type_names=["sample"]
    ) as template:
        template.add_properties({"Measurements": measurements})
        template.finalize()
    group = client.create_resource("Received samples", "Received container")
    sources = []
    for index in range(2):
        with client.build_resource(
            f"Source {index}", "Source sample", parent=group
        ) as builder:
            model = builder.get_model()
            model.properties["Measurements"].values["Mass"].value = 2.5 + index
            builder.set_model(model)
            builder.finalize()
        sources.append(builder.resource.id)
    with client.build_resource(resource_id=group.id) as builder:
        builder.finalize()
    with client.build_process_template("Manual Transfer", "1.0") as template:
        for name, resource_type, direction, required in (
            ("group", "container", Direction.input, True),
            ("plate", "plate", Direction.output, True),
        ):
            template.add_resource_slot(
                name, resource_type, direction, required=required
            )
        step = template.add_step("Transfer").add_parameters(
            {
                "Transfer details": [
                    {"name": "Operator", "type": "str", "default": None},
                    {"name": "Timestamp", "type": "str", "default": None},
                    {"name": "Volume", "type": "float", "unit": "uL", "default": None},
                ]
            }
        )
        step.bind_slot("source", "group")
        step.bind_slot("destination", "plate")
        step.close_step()
        template.finalize()
    return PlateWorkflowInputs(group.id, (sources[0], sources[1]), well_names)


def record_loaded_plate(client, inputs, *, measure=nullcontext):
    """Record two transfers with explicit source coverage and per-command commits."""
    client.connection_state.backend.identity_map.clear()
    with measure("source_reads"):
        group = client.get_resource(
            "Received samples", "Received container", expand=True
        )
        sources = [
            client.get_resource(f"Source {index}", "Source sample", expand=True)
            for index in range(2)
        ]
        assert group.id == inputs.group_id
        assert tuple(source.id for source in sources) == inputs.source_ids
        assert all(source.parent.is_loaded("properties") for source in sources)
    with (
        measure("create_plate"),
        client.build_resource("Recorded plate", "Loaded plate") as builder,
    ):
        pass
    plate_id = builder.resource.id
    with measure("children_read"):
        plate = client.get_resource("Recorded plate", "Loaded plate", expand=True)
    selected_names = (
        inputs.well_names[0],
        inputs.well_names[min(12, len(inputs.well_names) - 1)],
    )
    for index, (name, source) in enumerate(zip(selected_names, sources, strict=True)):
        with (
            measure(f"update_well_{index}"),
            client.build_resource(resource_id=plate.children[name].id) as builder,
        ):
            model = builder.get_model()
            values = model.properties["Measurements"].values
            values["Mass"].value = (
                source.properties["Measurements"].values["Mass"].value
            )
            values["Mass"].metadata_json = {"source_id": str(source.id)}
            values["Readings"].value = (
                source.properties["Measurements"].values["Readings"].value
            )
            model.properties["Identity"].values["Note"].value = (
                source.parent.properties["Receipt"].values["Batch"].value
            )
            model.properties["Identity"].values["Label"].value = source.name
            model.properties["Loading"].values["Volume"].value = 10.0
            builder.set_model(model)
            builder.finalize()
    with measure("finalize_plate"):
        with client.build_resource(resource_id=plate_id) as builder:
            builder.finalize()
        plate = builder.resource
    with (
        measure("create_run"),
        client.build_process_run(
            "Recorded transfer", "Two transfers", "Manual Transfer", "1.0"
        ) as builder,
    ):
        builder.assign_resource("group", group)
        builder.assign_resource("plate", plate)
    run_id = builder.process_run.id
    with (
        measure("finalize_run"),
        client.build_process_run(process_run_id=run_id) as builder,
    ):
        parent = builder.process_run.steps["Transfer"]
        for name, source in zip(selected_names, sources, strict=True):
            child = parent.generate_child(name=name)
            child.resources["source"] = source
            child.resources["destination"] = plate.children[name]
            values = child.parameters["Transfer details"].values
            values["Operator"].value = (
                source.parent.properties["Receipt"].values["Operator"].value
            )
            values["Timestamp"].value = "2026-10-23T12:00:00Z"
            values["Volume"].value = 10.0
            builder.add_child_step(child)
        builder.finalize()
    return LoadedPlateGraph(
        client.namespace_path,
        plate_id,
        tuple(plate.children[name].id for name in inputs.well_names),
        inputs.source_ids,
        run_id,
    )
