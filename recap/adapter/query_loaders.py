from pydantic import BaseModel

from recap.adapter.schema_registry import SCHEMA_REGISTRY
from recap.db.attribute import AttributeGroupTemplate, AttributeValue
from recap.db.process import (
    ProcessRun,
    ProcessTemplate,
    ResourceAssignment,
    ResourceSlot,
)
from recap.db.resource import Property, Resource, ResourceTemplate
from recap.db.step import Parameter, Step, StepTemplate, StepTemplateResourceSlotBinding
from recap.schemas.process import (
    ProcessRunRef,
    ProcessRunSchema,
    ProcessTemplateSchema,
)
from recap.schemas.resource import ResourceRef, ResourceSchema, ResourceTemplateSchema
from recap.utils.loaders import chain_load


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def property_loader_options(*prefix) -> list:
    """Load property/value collection keys with their rows, preserving batching."""
    return [
        chain_load(
            *prefix,
            Resource.properties,
            Property._values,
            AttributeValue.template,
            joined=(AttributeValue.template,),
        ),
        chain_load(
            *prefix,
            Resource.properties,
            Property.template,
            AttributeGroupTemplate.attribute_templates,
            joined=(Property.template,),
        ),
    ]


PRELOAD_STATEMENTS = {
    (ProcessRunSchema, "template"): [
        chain_load(ProcessRun.template, ProcessTemplate.resource_slots),
        chain_load(
            ProcessRun.template,
            ProcessTemplate.step_templates,
            StepTemplate.attribute_group_templates,
            AttributeGroupTemplate.attribute_templates,
        ),
        chain_load(
            ProcessRun.template,
            ProcessTemplate.step_templates,
            StepTemplate.bindings,
            StepTemplateResourceSlotBinding.resource_slot,
            ResourceSlot.resource_type,
        ),
    ],
    (ProcessRunSchema, "steps"): [
        chain_load(ProcessRun.steps, Step.assignments, ResourceAssignment.resource),
        chain_load(ProcessRun.steps, Step.children),
        chain_load(
            ProcessRun.steps,
            Step.parameters,
            Parameter.template,
            joined=(Parameter.template,),
        ),
        chain_load(
            ProcessRun.steps,
            Step.template,
            StepTemplate.attribute_group_templates,
            AttributeGroupTemplate.attribute_templates,
        ),
        chain_load(
            ProcessRun.steps,
            Step.template,
            StepTemplate.bindings,
            StepTemplateResourceSlotBinding.resource_slot,
            ResourceSlot.resource_type,
        ),
    ],
    (ProcessRunSchema, "steps.parameters"): [
        chain_load(
            ProcessRun.steps,
            Step.parameters,
            Parameter._values,
            AttributeValue.template,
            joined=(AttributeValue.template,),
        ),
        chain_load(
            ProcessRun.steps,
            Step.parameters,
            Parameter.template,
            AttributeGroupTemplate.attribute_templates,
            joined=(Parameter.template,),
        ),
    ],
    (ProcessRunSchema, "resources"): [
        chain_load(ProcessRun.assignments, ResourceAssignment.resource),
        chain_load(
            ProcessRun.assignments,
            ResourceAssignment.resource_slot,
            ResourceSlot.resource_type,
            joined=(ResourceAssignment.resource_slot,),
        ),
        chain_load(
            ProcessRun.assignments,
            ResourceAssignment.resource,
            Resource.template,
            ResourceTemplate.types,
        ),
        *property_loader_options(ProcessRun.assignments, ResourceAssignment.resource),
    ],
    (ProcessTemplateSchema, "step_templates"): [
        chain_load(ProcessTemplate.step_templates)
    ],
    (ProcessTemplateSchema, "resource_slots"): [
        chain_load(ProcessTemplate.resource_slots)
    ],
    (ResourceTemplateSchema, "children"): [chain_load(ResourceTemplate.children)],
    (ResourceTemplateSchema, "attribute_group_templates"): [
        chain_load(ResourceTemplate.attribute_group_templates)
    ],
    (ResourceTemplateSchema, "types"): [chain_load(ResourceTemplate.types)],
    (ResourceSchema, "properties"): property_loader_options(),
    (ResourceRef, "properties"): property_loader_options(),
    (ResourceSchema, "children"): [chain_load(Resource.children)],
    (ResourceRef, "children"): [chain_load(Resource.children)],
    (ResourceSchema, "template"): [chain_load(Resource.template)],
    (ResourceRef, "template"): [chain_load(Resource.template)],
}

FULL_PRELOAD_PATHS = {
    ProcessRunSchema: ["steps", "steps.parameters", "resources"],
    ResourceSchema: ["template", "properties", "children"],
    ProcessTemplateSchema: ["step_templates", "resource_slots"],
    ResourceTemplateSchema: ["types", "children", "attribute_group_templates"],
}

BASE_SCHEMA_LOADERS = {
    ProcessRunRef: [chain_load(ProcessRun.template)],
    ResourceRef: [chain_load(Resource.template, ResourceTemplate.types)],
}

EXTRA_FULL_LOADERS = {
    ProcessRunSchema: [
        chain_load(
            ProcessRun.template,
            ProcessTemplate.resource_slots,
            ResourceSlot.resource_type,
        ),
        chain_load(
            ProcessRun.template,
            ProcessTemplate.step_templates,
            StepTemplate.attribute_group_templates,
            AttributeGroupTemplate.attribute_templates,
        ),
        chain_load(
            ProcessRun.template,
            ProcessTemplate.step_templates,
            StepTemplate.bindings,
            StepTemplateResourceSlotBinding.resource_slot,
            ResourceSlot.resource_type,
        ),
    ],
    ResourceSchema: [
        chain_load(
            Resource.template,
            ResourceTemplate.attribute_group_templates,
            AttributeGroupTemplate.attribute_templates,
        ),
        chain_load(Resource.template, ResourceTemplate.parent, ResourceTemplate.types),
    ],
}


def preload_options(schema: type[BaseModel], name: str) -> list:
    try:
        return PRELOAD_STATEMENTS[(schema, name)]
    except KeyError as exc:
        raise ValueError(f"Unsupported preload '{name}' for {schema.__name__}") from exc


def resolve_loader_options(
    schema: type[BaseModel],
    preloads: list[str],
    load_mode: str | None,
) -> list:
    requested_preloads = list(preloads)
    opts = list(BASE_SCHEMA_LOADERS.get(schema, []))

    if load_mode == "eager":
        requested_preloads = FULL_PRELOAD_PATHS.get(schema, []) + requested_preloads
        opts.extend(EXTRA_FULL_LOADERS.get(schema, []))

    for preload in _dedupe_preserve_order(requested_preloads):
        canonical = {
            ResourceRef: ResourceSchema,
            ProcessRunRef: ProcessRunSchema,
        }.get(schema, schema)
        registration = SCHEMA_REGISTRY.by_model(canonical)
        if preload not in registration.loader_capabilities:
            raise ValueError(f"Unsupported preload '{preload}' for {schema.__name__}")
        opts.extend(preload_options(schema, preload))
    return opts
