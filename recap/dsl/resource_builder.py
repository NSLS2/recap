from typing import Optional

from sqlalchemy.orm import Session

from recap.dsl.attribute_builder import AttributeGroupBuilder
from recap.models.resource import ResourceTemplate, ResourceType
from recap.utils.dsl import _get_or_create


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

    def prop_group(self, group_name: str) -> AttributeGroupBuilder:
        agb = AttributeGroupBuilder(
            session=self.session, group_name=group_name, parent=self
        )
        return agb

    def add_child(self, name: str, type_names: list[str]):
        child_builder = ResourceTemplateBuilder(
            self.session, name=name, type_names=type_names, parent=self
        )
        return child_builder

    def close_child(self):
        if self.parent:
            return self.parent
        else:
            return self
