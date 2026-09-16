from typing import Any, Literal, TypeVar

from pydantic import BaseModel

from recap.adapter.entity_hydration import EntityHydrationContext
from recap.adapter.resource_construct import ResourceSchemaHydrator
from recap.db.process import ProcessRun, ProcessTemplate, ResourceSlot
from recap.db.resource import Resource
from recap.db.step import Parameter, StepTemplate
from recap.schemas.process import ProcessRunSchema, ProcessTemplateSchema
from recap.schemas.resource import ResourceAssignmentSchema, ResourceSchema
from recap.schemas.step import ParameterSchema, StepSchema, StepTemplateSchema

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class ProcessRunSchemaHydrator:
    def __init__(self, context: EntityHydrationContext | None = None):
        self._context = context or EntityHydrationContext()
        self._resource_hydrator = ResourceSchemaHydrator(self._context)

    def _construct_with_simple_fields(
        self,
        schema: type[SchemaT],
        source: Any,
        **overrides: Any,
    ) -> SchemaT:
        return self._context.construct_with_simple_fields(schema, source, **overrides)

    def _construct_step_template(
        self,
        template: StepTemplate,
        *,
        on_unloaded: Literal["silent", "warn", "raise"] = "warn",
    ) -> StepTemplateSchema:
        return self._context.construct_step_template(
            template, include_relations=True, on_unloaded=on_unloaded
        )

    def _construct_step_template_minimal(
        self,
        template: StepTemplate,
        *,
        on_unloaded: Literal["silent", "warn", "raise"] = "warn",
    ) -> StepTemplateSchema:
        return self._context.construct_step_template(
            template, include_relations=False, on_unloaded=on_unloaded
        )

    def _construct_process_template(
        self,
        template: ProcessTemplate,
        *,
        on_unloaded: Literal["silent", "warn", "raise"] = "warn",
    ) -> ProcessTemplateSchema:
        return self._context.construct_process_template(
            template, include_relations=True, on_unloaded=on_unloaded
        )

    def _construct_process_template_minimal(
        self,
        template: ProcessTemplate,
        *,
        on_unloaded: Literal["silent", "warn", "raise"] = "warn",
    ) -> ProcessTemplateSchema:
        return self._context.construct_process_template(
            template, include_relations=False, on_unloaded=on_unloaded
        )

    def _construct_parameter_schema(
        self,
        param: Parameter,
    ) -> ParameterSchema:
        return self._context.construct_parameter_schema(param)

    def _construct_resource_slot(self, slot: ResourceSlot):
        return self._context.construct_resource_slot(slot)

    def _construct_resource_schema(
        self,
        resource: Resource,
        children_map: dict[Any, list[Resource]] | None = None,
        *,
        on_unloaded: Literal["silent", "warn", "raise"] = "warn",
    ) -> ResourceSchema:
        return self._resource_hydrator._construct_resource_schema(
            resource,
            include_template=True,
            include_properties=True,
            include_children=True,
            full=True,
            on_unloaded=on_unloaded,
            children_map=children_map,
        )

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
        include_template: bool,
        full: bool,
        on_unloaded: Literal["silent", "warn", "raise"],
        children_map: dict[Any, list[Resource]] | None = None,
    ) -> ProcessRunSchema:
        template = (
            self._construct_process_template(run.template, on_unloaded=on_unloaded)
            if full or include_template
            else self._construct_process_template_minimal(
                run.template, on_unloaded=on_unloaded
            )
        )

        steps: dict[str, StepSchema] = {}
        step_models = run.steps.values() if include_steps else []
        for step in step_models:
            step_schema = self._construct_with_simple_fields(
                StepSchema,
                step,
                template=(
                    self._construct_step_template(
                        step.template, on_unloaded=on_unloaded
                    )
                    if full
                    else self._construct_step_template_minimal(
                        step.template, on_unloaded=on_unloaded
                    )
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
                        role: self._construct_resource_schema(
                            res, children_map, on_unloaded=on_unloaded
                        )
                        for role, res in step.resources.items()
                    }
                    if include_resources
                    else {}
                ),
            )
            step_schema.set_loaded_relations(
                {
                    "template": full,
                    "parameters": include_step_parameters,
                    "children": include_steps,
                    "resources": include_resources,
                },
                on_unloaded=on_unloaded,
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
                            assigned.resource,
                            children_map,
                            on_unloaded=on_unloaded,
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
                "template": full or include_template,
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
        include_template: bool,
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
                include_template=include_template,
                full=full,
                on_unloaded=on_unloaded,
                children_map=children_map,
            )
            for run in runs
        ]
