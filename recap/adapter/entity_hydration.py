from functools import lru_cache
from typing import Any, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel

from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.db.process import ProcessTemplate, ResourceSlot
from recap.db.resource import Property, ResourceType
from recap.db.step import Parameter, StepTemplate
from recap.schemas.attribute import (
    AttributeGroupTemplateSchema,
    AttributeTemplateSchema,
    AttributeValueSchema,
)
from recap.schemas.common import SIMPLE_FIELD, LoadAware
from recap.schemas.process import ProcessTemplateSchema
from recap.schemas.resource import (
    PropertySchema,
    ResourceSchema,
    ResourceSlotSchema,
    ResourceTemplateSchema,
    ResourceTypeSchema,
)
from recap.schemas.step import ParameterSchema, StepTemplateSchema
from recap.utils.dsl import build_param_values_model

SchemaT = TypeVar("SchemaT", bound=BaseModel)


@lru_cache(maxsize=128)
def _simple_field_mapping(
    schema: type[BaseModel],
) -> tuple[tuple[str, str | None], ...]:
    return tuple(
        (name, field.alias)
        for name, field in schema.model_fields.items()
        if SIMPLE_FIELD in field.metadata
    )


class EntityHydrationContext:
    """Shared caches and constructors for one backend hydration operation."""

    def __init__(self):
        self.process_template_cache: dict[UUID, ProcessTemplateSchema] = {}
        self.step_template_cache: dict[UUID, StepTemplateSchema] = {}
        self.resource_slot_cache: dict[UUID, ResourceSlotSchema] = {}
        self.resource_type_cache: dict[UUID, ResourceTypeSchema] = {}
        self.resource_template_cache: dict[UUID, ResourceTemplateSchema] = {}
        self.resource_cache: dict[UUID, ResourceSchema] = {}
        self.attr_group_cache: dict[UUID, AttributeGroupTemplateSchema] = {}
        self.attr_template_cache: dict[UUID, AttributeTemplateSchema] = {}
        self._in_progress: set[tuple[str, UUID]] = set()

    def in_progress(self, family: str, entity_id: UUID) -> bool:
        return (family, entity_id) in self._in_progress

    def begin(self, family: str, entity_id: UUID) -> None:
        self._in_progress.add((family, entity_id))

    def finish(self, family: str, entity_id: UUID) -> None:
        self._in_progress.discard((family, entity_id))

    @staticmethod
    def merge_load_state(
        model: LoadAware,
        updates: dict[str, bool],
        *,
        on_unloaded: Literal["silent", "warn", "raise"],
    ) -> None:
        private = getattr(model, "__pydantic_private__", None) or {}
        current = dict(private.get("_loaded_relations", {}))
        merged = dict(current)
        for relation, loaded in updates.items():
            merged[relation] = current.get(relation, False) or loaded
        model.set_loaded_relations(merged, on_unloaded=on_unloaded)

    def simple_field_values(self, schema: type[SchemaT], source: Any) -> dict[str, Any]:
        values = {}
        for field_name, alias in _simple_field_mapping(schema):
            if hasattr(source, field_name):
                values[field_name] = getattr(source, field_name)
            elif alias and hasattr(source, alias):
                values[field_name] = getattr(source, alias)
        return values

    def construct_with_simple_fields(
        self, schema: type[SchemaT], source: Any, **overrides: Any
    ) -> SchemaT:
        values = self.simple_field_values(schema, source)
        values.update(overrides)
        return schema.model_construct(**values)

    def build_param_values_model(
        self, template: AttributeGroupTemplateSchema
    ) -> type[BaseModel]:
        key = tuple(
            (at.name, at.slug, at.value_type, at.metadata, at.unit)
            for at in template.attribute_templates
        )
        return build_param_values_model(template.slug or template.name, key)

    def construct_attribute_template(
        self, template: AttributeTemplate
    ) -> AttributeTemplateSchema:
        cached = self.attr_template_cache.get(template.id)
        if cached is None:
            cached = AttributeTemplateSchema.model_validate(template, from_attributes=True)
            self.attr_template_cache[template.id] = cached
        return cached

    def construct_attribute_group_template(
        self, template: AttributeGroupTemplate
    ) -> AttributeGroupTemplateSchema:
        cached = self.attr_group_cache.get(template.id)
        if cached is not None:
            return cached
        cached = self.construct_with_simple_fields(
            AttributeGroupTemplateSchema, template, attribute_templates=[]
        )
        self.attr_group_cache[template.id] = cached
        cached.attribute_templates = [
            self.construct_attribute_template(item)
            for item in template.attribute_templates
        ]
        cached.set_loaded_relations({"attribute_templates": True})
        return cached

    def construct_resource_type(self, resource_type: ResourceType) -> ResourceTypeSchema:
        cached = self.resource_type_cache.get(resource_type.id)
        if cached is None:
            cached = ResourceTypeSchema.model_validate(resource_type, from_attributes=True)
            self.resource_type_cache[resource_type.id] = cached
        return cached

    def construct_resource_slot(self, slot: ResourceSlot) -> ResourceSlotSchema:
        cached = self.resource_slot_cache.get(slot.id)
        if cached is None:
            cached = ResourceSlotSchema.model_validate(slot, from_attributes=True)
            self.resource_slot_cache[slot.id] = cached
        return cached

    def construct_step_template(
        self,
        template: StepTemplate,
        *,
        include_relations: bool,
        on_unloaded: Literal["silent", "warn", "raise"],
    ) -> StepTemplateSchema:
        schema = self.step_template_cache.get(template.id)
        if schema is None:
            schema = self.construct_with_simple_fields(
                StepTemplateSchema,
                template,
                attribute_group_templates=[],
                resource_slots={},
            )
            schema.set_loaded_relations(
                {"attribute_group_templates": False, "resource_slots": False},
                on_unloaded=on_unloaded,
            )
            self.step_template_cache[template.id] = schema

        if not include_relations or self.in_progress("step_template", template.id):
            return schema

        self.begin("step_template", template.id)
        loaded_now: dict[str, bool] = {}
        try:
            if not schema.is_loaded("attribute_group_templates"):
                schema.attribute_group_templates = [
                    self.construct_attribute_group_template(item)
                    for item in template.attribute_group_templates
                ]
                loaded_now["attribute_group_templates"] = True
            if not schema.is_loaded("resource_slots"):
                schema.resource_slots = {
                    binding.role: self.construct_resource_slot(binding.resource_slot)
                    for binding in template.bindings.values()
                }
                loaded_now["resource_slots"] = True
            self.merge_load_state(schema, loaded_now, on_unloaded=on_unloaded)
            return schema
        finally:
            self.finish("step_template", template.id)

    def construct_process_template(
        self,
        template: ProcessTemplate,
        *,
        include_relations: bool,
        on_unloaded: Literal["silent", "warn", "raise"],
    ) -> ProcessTemplateSchema:
        schema = self.process_template_cache.get(template.id)
        if schema is None:
            schema = self.construct_with_simple_fields(
                ProcessTemplateSchema,
                template,
                step_templates={},
                resource_slots=[],
            )
            schema.set_loaded_relations(
                {"step_templates": False, "resource_slots": False},
                on_unloaded=on_unloaded,
            )
            self.process_template_cache[template.id] = schema

        if not include_relations or self.in_progress("process_template", template.id):
            return schema

        self.begin("process_template", template.id)
        loaded_now: dict[str, bool] = {}
        try:
            if not schema.is_loaded("resource_slots"):
                schema.resource_slots = [
                    self.construct_resource_slot(item) for item in template.resource_slots
                ]
                loaded_now["resource_slots"] = True
            if not schema.is_loaded("step_templates"):
                schema.step_templates = {
                    item.name: self.construct_step_template(
                        item,
                        include_relations=True,
                        on_unloaded=on_unloaded,
                    )
                    for item in template.step_templates.values()
                }
                loaded_now["step_templates"] = True
            self.merge_load_state(schema, loaded_now, on_unloaded=on_unloaded)
            return schema
        finally:
            self.finish("process_template", template.id)

    def construct_property_schema(self, prop: Property) -> PropertySchema:
        group = self.construct_attribute_group_template(prop.template)
        values_model = self.build_param_values_model(group)
        value_fields = {
            at.slug: AttributeValueSchema.model_construct(
                value=prop._values[at.name].value if at.name in prop._values else None,
                unit=prop._values[at.name].unit if at.name in prop._values else at.unit,
                metadata_json=(
                    prop._values[at.name].metadata_json
                    if at.name in prop._values
                    else {}
                ),
            )
            for at in group.attribute_templates
        }
        return self.construct_with_simple_fields(
            PropertySchema,
            prop,
            template=group,
            values=values_model.model_construct(**value_fields),
        )

    def construct_parameter_schema(self, param: Parameter) -> ParameterSchema:
        group = self.construct_attribute_group_template(param.template)
        values_model = self.build_param_values_model(group)
        value_fields = {
            at.slug: AttributeValueSchema.model_construct(
                value=param._values[at.name].value if at.name in param._values else None,
                unit=param._values[at.name].unit if at.name in param._values else at.unit,
                metadata_json=(
                    param._values[at.name].metadata_json
                    if at.name in param._values
                    else {}
                ),
            )
            for at in group.attribute_templates
        }
        return self.construct_with_simple_fields(
            ParameterSchema,
            param,
            template=group,
            values=values_model.model_construct(**value_fields),
        )
