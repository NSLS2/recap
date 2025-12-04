from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, create_model

from recap.adapter import Backend
from recap.db.resource import Resource
from recap.dsl.attribute_builder import AttributeGroupBuilder
from recap.dsl.process_builder import map_dtype_to_pytype
from recap.schemas.attribute import AttributeTemplateValidator
from recap.schemas.resource import (
    ResourceSchema,
    ResourceTemplateRef,
    ResourceTypeSchema,
)
from recap.utils.dsl import AliasMixin


class ResourceBuilder:
    def __init__(
        self,
        # session: Session,
        name: str,
        template_name: str,
        backend: Backend,
        create_new: bool = False,
        parent: Optional["ResourceBuilder"] = None,
    ):
        self.name = name
        self._children: list[Resource] = []
        self.parent = parent
        self.parent_resource = parent._resource if parent else None
        self.backend = backend
        self.create_new = create_new
        self.template_name = template_name

    @classmethod
    def create(cls, name: str, template_name: str, backend: Backend, parent=None):
        with cls(name, template_name, backend, create_new=True, parent=parent) as rb:
            return rb.resource

    def __enter__(self):
        self._uow = self.backend.begin()

        if self.create_new:
            template = self.backend.get_resource_template(name=self.template_name)
            self._resource = self.backend.create_resource(
                self.name,
                resource_template=template,
                parent_resource=self.parent_resource,
                expand=True,
            )
        else:
            self._resource = self.backend.get_resource(self.name, self.template_name)
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.save()
        else:
            self._uow.rollback()

    def save(self):
        self._uow.commit()
        return self

    @property
    def resource(self) -> ResourceSchema:
        if self._resource is None:
            raise RuntimeError(
                "Call .save() first or construct resource via builder methods"
            )
        return self._resource

    def add_child(self, name: str, template_name: str) -> "ResourceBuilder":
        child_builder = ResourceBuilder(
            name=name,
            template_name=template_name,
            parent=self,
            backend=self.backend,
            create_new=True,
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
        parent: Optional["ResourceTemplateBuilder"] = None,
        backend: Backend | None = None,
    ):
        if backend:
            self.backend = backend
            self._uow = self.backend.begin()
        elif parent:
            self.backend = parent.backend
        else:
            raise ValueError("No parent builder or backend provided")
        self.name = name
        self.type_names = type_names
        self._children: list[ResourceTemplateRef] = []
        self.parent = parent
        self.resource_types: dict[str, ResourceTypeSchema] = {}
        for rt_schema in self.backend.add_resource_types(type_names):
            self.resource_types[rt_schema.name] = rt_schema
        if self.parent:
            self._template = self.backend.add_child_resource_template(
                self.name,
                [rt for rt in self.resource_types.values()],
                parent_resource_template=self.parent._template,
            )
        else:
            self._template: ResourceTemplateRef = self.backend.add_resource_template(
                name, list(self.resource_types.values())
            )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.save()
        else:
            self._uow.rollback()

    def save(self):
        self._uow.commit()
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

        for group_key, props in prop_def.items():
            agb = AttributeGroupBuilder(group_name=group_key, parent=self)
            for prop in props:
                attr = AttributeTemplateValidator.model_validate(prop)
                agb.add_attribute(attr.name, attr.type, attr.unit, attr.default)
            agb.close_group()
        return self

    def add_child(self, name: str, type_names: list[str]) -> "ResourceTemplateBuilder":
        child_builder = ResourceTemplateBuilder(
            name=name, type_names=type_names, parent=self
        )
        return child_builder

    def close_child(self):
        if self.parent:
            return self.parent
        else:
            return self
