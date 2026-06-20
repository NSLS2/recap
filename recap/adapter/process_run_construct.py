from functools import lru_cache
from typing import Any, Literal, TypeVar

from pydantic import BaseModel

from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.db.process import ProcessRun, ProcessTemplate, ResourceSlot
from recap.db.resource import Property, Resource, ResourceTemplate, ResourceType
from recap.db.step import Parameter, StepTemplate
from recap.schemas.attribute import (
    AttributeGroupTemplateSchema,
    AttributeTemplateSchema,
    AttributeValueSchema,
)
from recap.schemas.common import SIMPLE_FIELD
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
from recap.utils.dsl import build_param_values_model

SchemaT = TypeVar("SchemaT", bound=BaseModel)


@lru_cache(maxsize=128)
def _simple_field_mapping(
    schema: type[BaseModel],
) -> tuple[tuple[str, str | None], ...]:
    return tuple(
        (field_name, field_info.alias)
        for field_name, field_info in schema.model_fields.items()
        if SIMPLE_FIELD in field_info.metadata
    )


class ProcessRunSchemaHydrator:
    def __init__(self):
        self._process_template_cache: dict = {}
        self._step_template_cache: dict = {}
        self._resource_slot_cache: dict = {}
        self._resource_type_cache: dict = {}
        self._resource_template_cache: dict = {}
        self._resource_template_ref_cache: dict = {}
        self._resource_ref_cache: dict = {}
        self._resource_cache: dict = {}
        self._attr_group_cache: dict = {}
        self._attr_template_cache: dict = {}

    def _simple_field_values(
        self,
        schema: type[SchemaT],
        source: Any,
    ) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for field_name, alias in _simple_field_mapping(schema):
            if hasattr(source, field_name):
                values[field_name] = getattr(source, field_name)
                continue
            if alias and hasattr(source, alias):
                values[field_name] = getattr(source, alias)
        return values

    def _construct_with_simple_fields(
        self,
        schema: type[SchemaT],
        source: Any,
        **overrides: Any,
    ) -> SchemaT:
        values = self._simple_field_values(schema, source)
        values.update(overrides)
        return schema.model_construct(**values)

    def _build_param_values_model_from_template(
        self,
        template: AttributeGroupTemplateSchema,
    ) -> type[BaseModel]:
        tmpl_key = tuple(
            (
                at.name,
                at.slug,
                at.value_type,
                at.metadata,
                at.unit,
            )
            for at in template.attribute_templates
        )
        return build_param_values_model(template.slug or template.name, tmpl_key)

    def _construct_attribute_template(
        self,
        attr_template: AttributeTemplate,
    ) -> AttributeTemplateSchema:
        cached = self._attr_template_cache.get(attr_template.id)
        if cached is not None:
            return cached
        schema = AttributeTemplateSchema.model_validate(
            attr_template, from_attributes=True
        )
        self._attr_template_cache[attr_template.id] = schema
        return schema

    def _construct_attribute_group_template(
        self,
        group_template: AttributeGroupTemplate,
    ) -> AttributeGroupTemplateSchema:
        cached = self._attr_group_cache.get(group_template.id)
        if cached is not None:
            return cached
        schema = self._construct_with_simple_fields(
            AttributeGroupTemplateSchema,
            group_template,
            attribute_templates=[],
        )
        self._attr_group_cache[group_template.id] = schema
        schema.attribute_templates = [
            self._construct_attribute_template(at)
            for at in group_template.attribute_templates
        ]
        return schema

    def _construct_resource_type(
        self,
        resource_type: ResourceType,
    ) -> ResourceTypeSchema:
        cached = self._resource_type_cache.get(resource_type.id)
        if cached is not None:
            return cached
        schema = ResourceTypeSchema.model_validate(resource_type, from_attributes=True)
        self._resource_type_cache[resource_type.id] = schema
        return schema

    def _construct_resource_template_ref(
        self,
        template: ResourceTemplate,
    ) -> ResourceTemplateRef:
        cached = self._resource_template_ref_cache.get(template.id)
        if cached is not None:
            return cached
        schema = self._construct_with_simple_fields(
            ResourceTemplateRef,
            template,
            parent=None,
            types=[],
        )
        self._resource_template_ref_cache[template.id] = schema
        schema.types = [self._construct_resource_type(rt) for rt in template.types]
        if template.parent is not None:
            schema.parent = self._construct_resource_template_ref(template.parent)
        return schema

    def _construct_resource_slot(
        self,
        slot: ResourceSlot,
    ) -> ResourceSlotSchema:
        cached = self._resource_slot_cache.get(slot.id)
        if cached is not None:
            return cached
        schema = ResourceSlotSchema.model_validate(slot, from_attributes=True)
        self._resource_slot_cache[slot.id] = schema
        return schema

    def _construct_step_template(
        self,
        template: StepTemplate,
    ) -> StepTemplateSchema:
        cached = self._step_template_cache.get(template.id)
        if cached is not None:
            return cached
        schema = self._construct_with_simple_fields(
            StepTemplateSchema,
            template,
            attribute_group_templates=[],
            resource_slots={},
        )
        self._step_template_cache[template.id] = schema
        schema.attribute_group_templates = [
            self._construct_attribute_group_template(ag)
            for ag in template.attribute_group_templates
        ]
        schema.resource_slots = {
            binding.role: self._construct_resource_slot(binding.resource_slot)
            for binding in template.bindings.values()
        }
        return schema

    def _construct_step_template_minimal(
        self,
        template: StepTemplate,
    ) -> StepTemplateSchema:
        return self._construct_with_simple_fields(
            StepTemplateSchema,
            template,
            attribute_group_templates=[],
            resource_slots={},
        )

    def _construct_process_template(
        self,
        template: ProcessTemplate,
    ) -> ProcessTemplateSchema:
        cached = self._process_template_cache.get(template.id)
        if cached is not None:
            return cached
        schema = self._construct_with_simple_fields(
            ProcessTemplateSchema,
            template,
            step_templates={},
            resource_slots=[],
        )
        self._process_template_cache[template.id] = schema
        schema.resource_slots = [
            self._construct_resource_slot(slot) for slot in template.resource_slots
        ]
        schema.step_templates = {
            st.name: self._construct_step_template(st)
            for st in template.step_templates.values()
        }
        return schema

    def _construct_process_template_minimal(
        self,
        template: ProcessTemplate,
    ) -> ProcessTemplateSchema:
        return self._construct_with_simple_fields(
            ProcessTemplateSchema,
            template,
            step_templates={},
            resource_slots=[],
        )

    def _construct_resource_template(
        self,
        template: ResourceTemplate,
    ) -> ResourceTemplateSchema:
        cached = self._resource_template_cache.get(template.id)
        if cached is not None:
            return cached
        schema = self._construct_with_simple_fields(
            ResourceTemplateSchema,
            template,
            types=[],
            parent=None,
            children={},
            attribute_group_templates=[],
        )
        self._resource_template_cache[template.id] = schema
        schema.types = [self._construct_resource_type(rt) for rt in template.types]
        if template.parent is not None:
            schema.parent = self._construct_resource_template_ref(template.parent)
        schema.children = {
            child.name: self._construct_resource_template(child)
            for child in template.children.values()
        }
        schema.attribute_group_templates = [
            self._construct_attribute_group_template(ag)
            for ag in template.attribute_group_templates
        ]
        return schema

    def _construct_resource_ref(
        self,
        resource: Resource,
    ) -> ResourceRef:
        cached = self._resource_ref_cache.get(resource.id)
        if cached is not None:
            return cached
        schema = self._construct_with_simple_fields(
            ResourceRef,
            resource,
            template=self._construct_resource_template_ref(resource.template),
        )
        self._resource_ref_cache[resource.id] = schema
        return schema

    def _construct_property_schema(
        self,
        prop: Property,
    ) -> PropertySchema:
        group_template = self._construct_attribute_group_template(prop.template)
        values_model = self._build_param_values_model_from_template(group_template)
        value_fields = {
            at.slug: AttributeValueSchema.model_construct(
                value=prop._values[at.name].value if at.name in prop._values else None,
                unit=prop._values[at.name].unit if at.name in prop._values else at.unit,
            )
            for at in group_template.attribute_templates
        }
        return self._construct_with_simple_fields(
            PropertySchema,
            prop,
            template=group_template,
            values=values_model.model_construct(**value_fields),
        )

    def _construct_parameter_schema(
        self,
        param: Parameter,
    ) -> ParameterSchema:
        group_template = self._construct_attribute_group_template(param.template)
        values_model = self._build_param_values_model_from_template(group_template)
        value_fields = {
            at.slug: AttributeValueSchema.model_construct(
                value=param._values[at.name].value
                if at.name in param._values
                else None,
                unit=param._values[at.name].unit
                if at.name in param._values
                else at.unit,
            )
            for at in group_template.attribute_templates
        }
        return self._construct_with_simple_fields(
            ParameterSchema,
            param,
            template=group_template,
            values=values_model.model_construct(**value_fields),
        )

    def _construct_resource_schema(
        self,
        resource: Resource,
        children_map: dict[Any, list[Resource]] | None = None,
    ) -> ResourceSchema:
        cached = self._resource_cache.get(resource.id)
        if cached is not None:
            return cached
        schema = self._construct_with_simple_fields(
            ResourceSchema,
            resource,
            template=self._construct_resource_template(resource.template),
            parent=None,
            children={},
            properties={},
        )
        self._resource_cache[resource.id] = schema
        if resource.parent is not None:
            schema.parent = self._construct_resource_ref(resource.parent)
        # When a pre-assembled ``children_map`` is supplied, build children
        # from it instead of walking ``resource.children`` -- the latter
        # re-triggers a lazy load per node (the N+1 this path fixes).
        child_resources = (
            children_map.get(resource.id, [])
            if children_map is not None
            else resource.children.values()
        )
        schema.children = {
            child.name: self._construct_resource_schema(child, children_map)
            for child in child_resources
        }
        schema.properties = {
            prop.template.name: self._construct_property_schema(prop)
            for prop in resource.properties.values()
        }
        return schema

    def _post_build_dynamic_models(  # noqa
        self,
        process_run: ProcessRunSchema,
        *,
        include_step_parameters: bool,
        include_resources: bool,
    ) -> ProcessRunSchema:
        if not include_step_parameters and not include_resources:
            return process_run
        seen_resources: set[Any] = set()
        seen_steps: set[Any] = set()

        def materialize_resource_models(resource: ResourceSchema):
            resource_id = getattr(resource, "id", None)
            if resource_id in seen_resources:
                return
            seen_resources.add(resource_id)
            resource.build_property_model()
            for child in resource.children.values():
                materialize_resource_models(child)

        if include_resources:
            for assignment in process_run.assigned_resources.values():
                materialize_resource_models(assignment.resource)
        for step in process_run.steps.values():
            step_id = getattr(step, "id", None)
            if include_step_parameters and step_id not in seen_steps:
                step.build_parameter_model()
                seen_steps.add(step_id)
            if include_resources:
                for resource in step.resources.values():
                    materialize_resource_models(resource)
        return process_run

    def _construct_process_run_schema(
        self,
        run: ProcessRun,
        *,
        include_steps: bool,
        include_step_parameters: bool,
        include_resources: bool,
        full: bool,
        on_unloaded: Literal["silent", "warn", "raise"],
        children_map: dict[Any, list[Resource]] | None = None,
    ) -> ProcessRunSchema:
        template = (
            self._construct_process_template(run.template)
            if full
            else self._construct_process_template_minimal(run.template)
        )

        steps: dict[str, StepSchema] = {}
        step_models = run.steps.values() if include_steps else []
        for step in step_models:
            step_schema = self._construct_with_simple_fields(
                StepSchema,
                step,
                template=(
                    self._construct_step_template(step.template)
                    if full
                    else self._construct_step_template_minimal(step.template)
                ),
                parameters=(
                    {
                        param.template.name: self._construct_parameter_schema(param)
                        for param in step.parameters.values()
                    }
                    if include_step_parameters
                    else {}
                ),
                children=[],
                resources=(
                    {
                        role: self._construct_resource_schema(res, children_map)
                        for role, res in step.resources.items()
                    }
                    if include_resources
                    else {}
                ),
            )
            steps[step.name] = step_schema

        if include_steps:
            id_to_step = {step.id: steps[step.name] for step in run.steps.values()}
            for step in run.steps.values():
                step_schema = steps[step.name]
                step_schema.children = [
                    id_to_step[child.id]
                    for child in step.children
                    if child.id in id_to_step
                ]

        assigned_resources = {}
        if include_resources:
            for assigned in run.assigned_resources:
                assigned_resources[assigned.slot.name] = (
                    ResourceAssignmentSchema.model_construct(
                        slot=self._construct_resource_slot(assigned.slot),
                        resource=self._construct_resource_schema(
                            assigned.resource, children_map
                        ),
                        step_id=None,
                    )
                )

        process_run = self._construct_with_simple_fields(
            ProcessRunSchema,
            run,
            template=template,
            steps=steps,
            assigned_resources=assigned_resources,
        )
        process_run.set_loaded_relations(
            {
                "steps": include_steps,
                "assigned_resources": include_resources,
            },
            on_unloaded=on_unloaded,
        )
        return self._post_build_dynamic_models(
            process_run,
            include_step_parameters=include_step_parameters,
            include_resources=include_resources,
        )

    def construct_many(
        self,
        runs: list[ProcessRun],
        *,
        include_steps: bool,
        include_step_parameters: bool,
        include_resources: bool,
        full: bool,
        on_unloaded: Literal["silent", "warn", "raise"],
        children_map: dict[Any, list[Resource]] | None = None,
    ) -> list[ProcessRunSchema]:
        return [
            self._construct_process_run_schema(
                run,
                include_steps=include_steps,
                include_step_parameters=include_step_parameters,
                include_resources=include_resources,
                full=full,
                on_unloaded=on_unloaded,
                children_map=children_map,
            )
            for run in runs
        ]
