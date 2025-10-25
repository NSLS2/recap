from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, create_model
from sqlalchemy import select
from sqlalchemy.orm import Session

from recap.dsl.attribute_builder import AttributeGroupBuilder
from recap.dsl.process_builder import map_dtype_to_pytype
from recap.models.resource import Resource, ResourceTemplate, ResourceType
from recap.utils.database import _load_single
from recap.utils.dsl import AliasMixin, _get_or_create


class ResourceBuilder:
    def __init__(
        self,
        session: Session,
        name: str,
        template_name: str,
        create: bool = False,
        parent: Optional["ResourceBuilder"] = None,
    ):
        self.session = session
        self._tx = (
            self.session.begin_nested()
            if self.session.in_transaction()
            else self.session.begin()
        )
        self.name = name
        self._children: list[Resource] = []
        self.parent = parent
        statement = select(ResourceTemplate).where(
            ResourceTemplate.name == template_name
        )
        # self._template = self.session.scalars(statement).one()
        self._template = _load_single(session, statement, label="ResourceTemplate")
        if create:
            self._resource = Resource(name=self.name, template=self._template)
        else:
            statement = (
                select(Resource)
                .join(Resource.template)
                .where(
                    Resource.name == name,
                    ResourceTemplate.name == template_name,
                    Resource.active.is_(True),
                )
            )
            # self._resource = self.session.scalars(statement).one()
            self._resource = _load_single(session, statement, label="Resource")
        self.session.add(self._resource)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.save()
        else:
            self._tx.rollback()

    def save(self):
        self.session.add(self._resource)
        self.session.flush()
        self._tx.commit()
        return self

    @property
    def resource(self) -> Resource:
        if self._resource is None:
            raise RuntimeError(
                "Call .save() first or construct resource via builder methods"
            )
        return self._resource

    def add_child(self, name: str, template_name: str) -> "ResourceBuilder":
        child_builder = ResourceBuilder(
            self.session, name=name, template_name=template_name, parent=self
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
        session: Session,
        name: str,
        type_names: list[str],
        parent: Optional["ResourceTemplateBuilder"] = None,
    ):
        self.session = session
        self._tx = (
            self.session.begin_nested()
            if self.session.in_transaction()
            else self.session.begin()
        )
        self.name = name
        self.type_names = type_names
        self._children: list[ResourceTemplate] = []
        self.parent = parent
        self.resource_types = {}
        for type_name in self.type_names:
            where = {"name": type_name}
            resource_type, _ = _get_or_create(self.session, ResourceType, where=where)
            self.resource_types[type_name] = resource_type
        self._template: ResourceTemplate = ResourceTemplate(
            name=name,
            types=[rt for rt in self.resource_types.values()],
        )
        if self.parent:
            self._template.parent = self.parent._template
        self.session.add(self._template)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.save()
        else:
            self._tx.rollback()

    def save(self):
        self.session.add(self._template)
        self.session.flush()
        self._tx.commit()
        return self

    @property
    def template(self) -> ResourceTemplate:
        if self._template is None:
            raise RuntimeError(
                "Call .save() first or construct template via builder methods"
            )
        return self._template

    def _ensure_template(self):
        if self._template:
            return
        where = {"name": self.name}
        template, _ = _get_or_create(self.session, ResourceTemplate, where=where)
        self._template = template

    def prop_group(
        self, group_name: str
    ) -> AttributeGroupBuilder["ResourceTemplateBuilder"]:
        agb = AttributeGroupBuilder(
            session=self.session, group_name=group_name, parent=self
        )
        return agb

    def add_child(self, name: str, type_names: list[str]) -> "ResourceTemplateBuilder":
        child_builder = ResourceTemplateBuilder(
            self.session, name=name, type_names=type_names, parent=self
        )
        return child_builder

    def close_child(self):
        if self.parent:
            return self.parent
        else:
            return self
