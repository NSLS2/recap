from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, create_model

from recap.adapter import Backend
from recap.db.resource import Resource
from recap.dsl.attribute_builder import AttributeGroupBuilder
from recap.dsl.query import QuerySpec
from recap.schemas.attribute import AttributeTemplateValidator
from recap.schemas.resource import (
    ResourceSchema,
    ResourceTemplateRef,
    ResourceTemplateSchema,
    ResourceTypeSchema,
)
from recap.utils.dsl import AliasMixin, lock_instance_fields, map_dtype_to_pytype


class ResourceBuilder:
    def __init__(
        self,
        # session: Session,
        name: str,
        template_name: str,
        template_version: str = "1.0",
        backend: Backend | None = None,
        parent: "ResourceBuilder | ResourceSchema | None" = None,
        resource: ResourceSchema | None = None,
    ):
        self.name = name
        self._children: list[Resource] = []
        self.parent = None
        self.parent_resource = None
        if isinstance(parent, self.__class__):
            self.parent = parent
            self.parent_resource = parent._resource if parent else None
        elif isinstance(parent, ResourceSchema):
            self.parent_resource = parent
        self.template_name = template_name
        self.template_version = template_version
        self._resource: ResourceSchema | None = None
        self._uow = None
        if backend:
            self.backend = backend
            self._ensure_uow()
        elif self.parent:
            self.backend = self.parent.backend
            self.parent._ensure_uow()
            self._uow = self.parent._uow
        else:
            raise ValueError("backend is required")
        try:
            if resource is not None:
                self._resource = self._reload_resource(resource.id)
                self.name = self._resource.name
                self.template_name = self._resource.template.name
                self.template_version = self._resource.template.version
            else:
                template = self.backend.get_resource_template(
                    name=self.template_name, version=self.template_version
                )
                self._resource = self.backend.create_resource(
                    self.name,
                    resource_template=template,
                    parent_resource=self.parent_resource,
                    expand=True,
                )
            if self.parent_resource:
                print(
                    f"adding child {self._resource.name} to {self.parent_resource.name}"
                )
                self.backend.add_child_resources(self.parent_resource, [self._resource])
        except Exception as e:
            print(f"Exception occured while creating resource: {e}")

    @classmethod
    def create(
        cls,
        name: str,
        template_name: str,
        template_version: str,
        backend: Backend,
        parent=None,
    ):
        with cls(name, template_name, template_version, backend, parent=parent) as rb:
            return rb.resource

    def __enter__(self):
        self._ensure_uow()
        if self._resource is not None:
            self._resource = self._reload_resource(self._resource.id)
            self.name = self._resource.name
            self.template_name = self._resource.template.name
            self.template_version = self._resource.template.version
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.persist()
            self.save()
        else:
            if self._uow:
                self._uow.rollback()
            self._uow = None

    def _ensure_uow(self):
        if self._uow is None:
            self._uow = self.backend.begin()
        return self._uow

    def save(self):
        self._ensure_uow()
        self._uow.commit(clear_session=False)
        self._resource = self.backend.get_resource(
            self.name,
            self.template_name,
            self.template_version,
            expand=True,
        )
        self._uow.end_session()
        self._uow = None
        return self

    def persist(self):
        if self._resource is None:
            raise RuntimeError("Resource not initialized")
        self._resource = self.backend.update_resource(self._resource)
        return self

    def _reload_resource(self, resource_id: UUID) -> ResourceSchema:
        resources = self.backend.query(
            ResourceSchema,
            QuerySpec(filters={"id": resource_id}, preloads=["children", "properties"]),
        )
        if not resources:
            raise ValueError(f"Resource with id {resource_id} not found")
        return resources[0]

    @property
    def resource(self) -> ResourceSchema:
        if self._resource is None:
            raise RuntimeError(
                "Call .save() first or construct resource via builder methods"
            )
        return self._resource

    def get_model(self, *, update: bool = False) -> ResourceSchema:
        """
        Return a pydantic model representing the resource, optionally reloading
        from the backend first. Critical fields are locked against mutation.
        """
        if update and self._resource:
            self._resource = self._reload_resource(self._resource.id)
        model = (self._resource or self.resource).model_copy(deep=True)
        return lock_instance_fields(
            model, {"id", "create_date", "modified_date", "slug", "template"}
        )

    def set_model(self, model: ResourceSchema):
        if self.resource.id != model.id:
            raise ValueError(
                "ID for this Resource does not match the builder's resource"
            )
        self._resource = model

    def add_child(
        self, name: str, template_name: str, template_version: str = "1.0"
    ) -> "ResourceBuilder":
        child_builder = ResourceBuilder(
            name=name,
            template_name=template_name,
            template_version=template_version,
            parent=self,
        )
        return child_builder

    def close_child(self):
        if self.parent:
            return self.parent
        else:
            return self

    def get_props(self) -> type[BaseModel]:
        props: dict[str, tuple] = {
            "resource_name": (
                Literal[self.resource.name],
                Field(default=self.resource.name),
            ),
            "resource_id": (UUID, Field(default=self.resource.id)),
        }
        for _, prop in self._resource.properties.items():
            prop_fields: dict[str, tuple] = {}
            for val_name, value in prop.values.items():
                value_template = None
                for vt in prop.template.attribute_templates:
                    if vt.name == val_name:
                        value_template = vt
                        break
                if value_template is None:
                    raise ValueError(f"Could not find value with {val_name}")
                pytype = map_dtype_to_pytype(value_template.value_type)
                prop_fields[value_template.slug] = (
                    pytype | None,
                    Field(default=value, alias=value_template.name),
                )
                prop_model = create_model(
                    f"{val_name}", **prop_fields, __base__=(AliasMixin, BaseModel)
                )
                props[prop.template.slug] = (
                    prop_model,
                    Field(default_factory=prop_model, alias=prop.template.name),
                )
        model = create_model(
            f"{self.resource.name}", **props, __base__=(AliasMixin, BaseModel)
        )
        return model()

    def set_props(self, filled_props):
        if self.resource is None:
            raise ValueError("Resource not setup")
        for prop in self.resource.properties.values():
            filled_prop = filled_props.get(prop.template.name)
            for value_name in self.resource.properties[prop.template.name].values:
                self.resource.properties[prop.template.name].values[value_name] = (
                    filled_prop.get(value_name)
                )


class ResourceTemplateBuilder:
    def __init__(
        self,
        name: str,
        type_names: list[str],
        version: str = "1.0",
        parent: Optional["ResourceTemplateBuilder"] = None,
        backend: Backend | None = None,
        resource_template: ResourceTemplateRef | ResourceTemplateSchema | None = None,
    ):
        self._uow = None
        if backend:
            self.backend = backend
            self._ensure_uow()
        elif parent:
            self.backend = parent.backend
            parent._ensure_uow()
            self._uow = parent._uow
        else:
            raise ValueError("No parent builder or backend provided")
        self.name = name
        self.type_names = type_names
        self._children: list[ResourceTemplateRef] = []
        self.parent = parent
        self.resource_types: dict[str, ResourceTypeSchema] = {}
        self.version = version
        self._template: ResourceTemplateRef | ResourceTemplateSchema | None = None
        try:
            if resource_template is not None:
                self.name = resource_template.name
                self.type_names = [rt.name for rt in resource_template.types]
                self.version = resource_template.version
                tmpl = self.backend.get_resource_template(
                    resource_template.name,
                    version=resource_template.version,
                    id=resource_template.id,
                    expand=True,
                )
                self._template = tmpl
                for rt_schema in tmpl.types:
                    self.resource_types[rt_schema.name] = rt_schema
            else:
                for rt_schema in self.backend.add_resource_types(type_names):
                    self.resource_types[rt_schema.name] = rt_schema
                if self.parent:
                    self._template = self.backend.add_child_resource_template(
                        self.name,
                        [rt for rt in self.resource_types.values()],
                        version=self.version,
                        parent_resource_template=self.parent._template,
                    )
                else:
                    self._template: ResourceTemplateRef = (
                        self.backend.add_resource_template(
                            name,
                            list(self.resource_types.values()),
                            version=self.version,
                        )
                    )
        except Exception as e:
            print(f"Exception occured when creating a ResourceTemplate: {e}")

    def __enter__(self):
        self._ensure_uow()
        if self._template is not None:
            self._reload_template()
            self.name = self._template.name
            self.type_names = [rt.name for rt in self._template.types]
            self.version = self._template.version
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.save()
        else:
            if self._uow:
                self._uow.rollback()
            self._uow = None

    def save(self):
        self._ensure_uow()
        self._uow.commit()
        self._uow = None
        return self

    @property
    def template(self) -> ResourceTemplateRef:
        if self._template is None:
            raise RuntimeError(
                "Call .save() first or construct template via builder methods"
            )
        return self._template

    def prop_group(
        self, group_name: str
    ) -> AttributeGroupBuilder["ResourceTemplateBuilder"]:
        self._ensure_uow()
        agb: AttributeGroupBuilder[ResourceTemplateBuilder] = AttributeGroupBuilder(
            group_name=group_name, parent=self
        )
        return agb

    def add_properties(
        self, prop_def: dict[str, list[dict[str, Any]]]
    ) -> "ResourceTemplateBuilder":
        """
        Add properties in the form of a dictionary, first level of keys
        represents groups which have a list of dictionaries representing properties
        {
            "content": [
                {"name": "catalog_id",
                "type": "str",
                "unit": "",
                "default": ""}
            ]
        }
        """
        self._ensure_uow()

        for group_key, props in prop_def.items():
            agb = AttributeGroupBuilder(group_name=group_key, parent=self)
            for prop in props:
                attr = AttributeTemplateValidator.model_validate(prop)
                agb.add_attribute(attr.name, attr.type, attr.unit, attr.default)
            agb.close_group()
        return self

    def add_child(
        self, name: str, type_names: list[str], version: str = "1.0"
    ) -> "ResourceTemplateBuilder":
        self._ensure_uow()
        child_builder = ResourceTemplateBuilder(
            name=name, type_names=type_names, version=version, parent=self
        )
        return child_builder

    def _reload_template(self):
        self._ensure_uow()
        self._template = self.backend.get_resource_template(
            self.name,
            version=self.version,
            id=self._template.id if self._template else None,
            expand=True,
        )

    def get_model(self, *, update: bool = False) -> ResourceTemplateSchema:
        """
        Return a pydantic model for the resource template, optionally reloading
        from the backend first. Critical fields are locked against mutation.
        """
        self._ensure_uow()
        if update and self._template:
            self._reload_template()
        model = self.backend.get_resource_template(
            self.name,
            version=self.version,
            id=self._template.id if self._template else None,
            expand=True,
        )
        return lock_instance_fields(
            model.model_copy(deep=True),
            {"id", "create_date", "modified_date", "version"},
        )

    def _ensure_uow(self):
        if self._uow is None:
            self._uow = self.backend.begin()
        return self._uow

    def close_child(self):
        if self.parent:
            return self.parent
        else:
            return self
