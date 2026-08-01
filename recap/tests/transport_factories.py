from datetime import UTC, datetime
from uuid import uuid4

from recap.lifecycle import LifecycleStatus
from recap.schemas.attribute import (
    AttributeGroupTemplateSchema,
    AttributeTemplateSchema,
)
from recap.schemas.common import StepStatus
from recap.schemas.process import ProcessRunSchema, ProcessTemplateSchema
from recap.schemas.resource import (
    PropertySchema,
    ResourceAssignmentSchema,
    ResourceRef,
    ResourceSchema,
    ResourceSlotSchema,
    ResourceTemplateRef,
    ResourceTemplateSchema,
    ResourceTypeSchema,
)
from recap.schemas.step import ParameterSchema, StepSchema, StepTemplateSchema
from recap.utils.general import Direction

STAMP = datetime(2026, 7, 23, 12, 30, tzinfo=UTC)


def fields(**values):
    return {
        "id": uuid4(),
        "create_date": STAMP,
        "modified_date": STAMP,
        "namespace_id": uuid4(),
        "status": LifecycleStatus.ACTIVE,
        "revision": 1,
        "labels": [],
        **values,
    }


def attribute_group(name: str = "Measurements") -> AttributeGroupTemplateSchema:
    attribute = AttributeTemplateSchema(
        **fields(
            name="Captured At",
            slug="captured_at",
            value_type="datetime",
            unit=None,
            default_value=STAMP,
            metadata={"source": "detector"},
        )
    )
    return AttributeGroupTemplateSchema(
        **fields(name=name, slug=name.lower(), attribute_templates=[attribute])
    )


def resource_template(
    group: AttributeGroupTemplateSchema | None = None,
) -> ResourceTemplateSchema:
    parent = ResourceTemplateRef(
        **fields(name="Container", slug="container", version="1.0", types=[])
    )
    return ResourceTemplateSchema(
        **fields(
            name="Sample",
            slug="sample",
            version="1.0",
            types=[],
            parent=parent,
            children={},
            attribute_group_templates=[] if group is None else [group],
        )
    )


def minimal_resource(name: str = "sample") -> ResourceSchema:
    resource = ResourceSchema(
        **fields(
            name=name,
            template=resource_template(),
            parent=None,
            children={},
            properties={},
        )
    )
    return resource.set_loaded_relations(
        {"children": False, "properties": False}, on_unloaded="silent"
    )


def full_resource() -> ResourceSchema:
    group = attribute_group()
    template = resource_template(group)
    parent_template = ResourceTemplateRef(
        **fields(name="Container", slug="container", version="1.0", types=[])
    )
    parent = ResourceRef(**fields(name="plate", template=parent_template))
    prop = PropertySchema(**fields(template=group, values={"Captured At": STAMP}))
    child = minimal_resource("child")
    resource = ResourceSchema(
        **fields(
            name="sample",
            template=template,
            parent=parent,
            children={"child": child},
            properties={"Measurements": prop},
        )
    )
    return resource.set_loaded_relations(
        {"children": True, "properties": True}, on_unloaded="raise"
    )


def full_process_run() -> ProcessRunSchema:
    group = attribute_group("Acquisition")
    resource_type = ResourceTypeSchema(**fields(name="sample"))
    slot = ResourceSlotSchema(
        **fields(
            name="input",
            resource_type=resource_type,
            direction=Direction.input,
            required=True,
        )
    )
    step_template = StepTemplateSchema(
        **fields(
            name="Acquire",
            attribute_group_templates=[group],
            resource_slots={"input": slot},
        )
    )
    process_template = ProcessTemplateSchema(
        **fields(
            name="Measurement",
            version="1.0",
            is_active=True,
            step_templates={"Acquire": step_template},
            resource_slots=[slot],
        )
    )
    parameter = ParameterSchema(**fields(template=group, values={"Captured At": STAMP}))
    resource = minimal_resource()
    run_id = uuid4()
    step = StepSchema(
        **fields(
            name="Acquire",
            template=step_template,
            parameters={"Acquisition": parameter},
            state=StepStatus.COMPLETE,
            process_run_id=run_id,
            resources={"input": resource},
        )
    )
    assignment = ResourceAssignmentSchema(slot=slot, resource=resource, step_id=step.id)
    run = ProcessRunSchema(
        **fields(
            id=run_id,
            name="run-1",
            description="transport round trip",
            template=process_template,
            steps={"Acquire": step},
            assigned_resources={"input": assignment},
        )
    )
    return run.set_loaded_relations(
        {"steps": True, "assigned_resources": True}, on_unloaded="raise"
    )
