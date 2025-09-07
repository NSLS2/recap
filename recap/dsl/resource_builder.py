import warnings
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from recap.models.attribute import AttributeTemplate, AttributeValueTemplate
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
        self._tx = session.begin_nested()
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
        self.close()

    def save(self):
        self.session.add(self._template)
        self.session.flush()
        self._tx.commit()
        return self

    def close(self):
        self.session.close()

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

    def add_prop(
        self,
        group_name: str,
        prop_name: str,
        value_type: str,
        unit: str,
        default: str | None = None,
        create_group=False,
    ) -> "ResourceTemplateBuilder":
        prop_group = self.session.execute(
            select(AttributeTemplate).filter_by(name=group_name)
        ).scalar_one_or_none()

        if prop_group is None:
            if create_group:
                prop_group = AttributeTemplate(name=group_name)
                self.session.add(prop_group)
                self.template.attribute_templates.append(prop_group)
                self.session.flush()
            else:
                raise ValueError(
                    f"Parameter group: {group_name} does not exist in database and create_group = False"
                )

        prop_value = self.session.execute(
            select(AttributeValueTemplate).filter_by(
                name=prop_name, value_type=value_type, attribute_template=prop_group
            )
        ).scalar_one_or_none()
        if prop_value is not None:
            warnings.warn(
                f"Property {prop_name} already exists for {group_name}", stacklevel=2
            )
        else:
            prop_value = AttributeValueTemplate(
                name=prop_name,
                value_type=value_type,
                attribute_template=prop_group,
                default_value=default,
                unit=unit,
            )
            self.session.add(prop_value)
            prop_group.value_templates.append(prop_value)
            self.session.flush()
        return self

    def remove_prop(self, group_name: str, prop_name: str) -> "ResourceTemplateBuilder":
        prop_group = self.session.execute(
            select(AttributeTemplate)
            .filter_by(name=group_name)
            .where(
                AttributeTemplate.resource_templates.any(
                    ResourceTemplate.id == self._template.id
                )
            )
        ).scalar_one_or_none()

        if prop_group is None:
            warnings.warn(f"Property group does not exist : {group_name}", stacklevel=2)
            return self

        prop_value = self.session.execute(
            select(AttributeValueTemplate).filter_by(
                name=prop_name, attribute_template=prop_group
            )
        ).scalar_one_or_none()
        if prop_value is None:
            warnings.warn(
                f"Property does not exist in group {group_name}: {prop_name}",
                stacklevel=2,
            )
            return self

        prop_group.value_templates.remove(prop_value)
        return self

    def add_child(self, name: str, type_names: list[str]):
        child_builder = ResourceTemplateBuilder(
            self.session, name=name, type_names=type_names, parent=self
        )
        return child_builder

    def complete_child(self):
        if self.parent:
            return self.parent
        else:
            return self
