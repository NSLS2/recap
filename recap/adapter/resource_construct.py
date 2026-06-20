from functools import lru_cache
from typing import Any, Literal, TypeVar

from pydantic import BaseModel

from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.db.resource import Property, Resource, ResourceTemplate, ResourceType
from recap.schemas.attribute import (
    AttributeGroupTemplateSchema,
    AttributeTemplateSchema,
    AttributeValueSchema,
)
from recap.schemas.common import SIMPLE_FIELD
from recap.schemas.resource import (
    PropertySchema,
    ResourceRef,
    ResourceSchema,
    ResourceTemplateRef,
    ResourceTemplateSchema,
    ResourceTypeSchema,
)
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


class ResourceSchemaHydrator:
    def __init__(self):
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

    def _construct_resource_template_minimal(
        self,
        template: ResourceTemplate,
    ) -> ResourceTemplateSchema:
        return self._construct_with_simple_fields(
            ResourceTemplateSchema,
            template,
            types=[],
            parent=None,
            children={},
            attribute_group_templates=[],
        )

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

    def _construct_resource_schema(
        self,
        resource: Resource,
        *,
        include_template: bool,
        include_properties: bool,
        include_children: bool,
        full: bool,
        on_unloaded: Literal["silent", "warn", "raise"],
        children_map: dict[Any, list[Resource]] | None = None,
    ) -> ResourceSchema:
        cached = self._resource_cache.get(resource.id)
        if cached is not None:
            return cached
        schema = self._construct_with_simple_fields(
            ResourceSchema,
            resource,
            template=(
                self._construct_resource_template(resource.template)
                if full or include_template
                else self._construct_resource_template_minimal(resource.template)
            ),
            parent=None,
            children={},
            properties={},
        )
        schema.set_loaded_relations(
            {
                "properties": include_properties,
                "children": include_children,
            },
            on_unloaded=on_unloaded,
        )
        self._resource_cache[resource.id] = schema
        if resource.parent is not None:
            schema.parent = self._construct_resource_ref(resource.parent)
        # When a pre-assembled ``children_map`` is supplied (flat-list
        # hydration), build children from it instead of walking
        # ``resource.children`` — the latter re-triggers a lazy load per node
        # (the N+1 this path fixes).
        if include_children:
            child_resources = (
                children_map.get(resource.id, [])
                if children_map is not None
                else resource.children.values()
            )
            schema.children = {
                child.name: self._construct_resource_schema(
                    child,
                    include_template=include_template,
                    include_properties=include_properties,
                    include_children=include_children,
                    full=full,
                    on_unloaded=on_unloaded,
                    children_map=children_map,
                )
                for child in child_resources
            }
        else:
            schema.children = {}
        schema.properties = (
            {
                prop.template.name: self._construct_property_schema(prop)
                for prop in resource.properties.values()
            }
            if include_properties
            else {}
        )
        return schema

    def _post_build_dynamic_models(
        self,
        resource: ResourceSchema,
        *,
        include_properties: bool,
        include_children: bool,
    ) -> ResourceSchema:
        if not include_properties and not include_children:
            return resource
        seen_resources: set[Any] = set()

        def materialize(item: ResourceSchema):
            resource_id = getattr(item, "id", None)
            if resource_id in seen_resources:
                return
            seen_resources.add(resource_id)
            if include_properties:
                item.build_property_model()
            if include_children:
                for child in item.children.values():
                    materialize(child)

        materialize(resource)
        return resource

    def construct_many(
        self,
        resources: list[Resource],
        *,
        include_template: bool,
        include_properties: bool,
        include_children: bool,
        full: bool,
        on_unloaded: Literal["silent", "warn", "raise"],
    ) -> list[ResourceSchema]:
        return [
            self._post_build_dynamic_models(
                self._construct_resource_schema(
                    resource,
                    include_template=include_template,
                    include_properties=include_properties,
                    include_children=include_children,
                    full=full,
                    on_unloaded=on_unloaded,
                ),
                include_properties=include_properties,
                include_children=include_children,
            )
            for resource in resources
        ]

    def construct_tree(
        self,
        flat_resources: list[Resource],
        root_ids: list[Any],
        *,
        include_template: bool,
        include_properties: bool,
        full: bool,
        on_unloaded: Literal["silent", "warn", "raise"],
    ) -> list[ResourceSchema]:
        """Hydrate resource trees from a **flat list** of roots + descendants.

        The parent->children map is assembled in Python by ``parent_id`` instead
        of walking lazy ``Resource.children`` relationships, so hydration issues
        zero per-node lazy loads. One schema is returned per id in ``root_ids``,
        in order.
        """
        children_map: dict[Any, list[Resource]] = {}
        for resource in flat_resources:
            parent_id = resource.parent_id
            if parent_id is not None:
                children_map.setdefault(parent_id, []).append(resource)

        by_id = {resource.id: resource for resource in flat_resources}
        results: list[ResourceSchema] = []
        for root_id in root_ids:
            root = by_id.get(root_id)
            if root is None:
                continue
            results.append(
                self._post_build_dynamic_models(
                    self._construct_resource_schema(
                        root,
                        include_template=include_template,
                        include_properties=include_properties,
                        include_children=True,
                        full=full,
                        on_unloaded=on_unloaded,
                        children_map=children_map,
                    ),
                    include_properties=include_properties,
                    include_children=True,
                )
            )
        return results
