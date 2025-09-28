import typing
import warnings
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, with_parent

from recap.models.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.models.resource import ResourceTemplate
from recap.models.step import StepTemplate

if typing.TYPE_CHECKING:
    from recap.dsl.process_builder import StepTemplateBuilder
    from recap.dsl.resource_builder import ResourceTemplateBuilder


class AttributeGroupBuilder:
    def __init__(
        self,
        session: Session,
        group_name: str,
        parent: "ResourceTemplateBuilder | StepTemplateBuilder",
    ):
        self.session = session
        self._tx = (
            self.session.begin_nested()
            if self.session.in_transaction()
            else self.session.begin()
        )
        self.group_name = group_name
        self.parent = parent
        self._template: AttributeGroupTemplate | None = self.session.execute(
            select(AttributeGroupTemplate).filter_by(name=group_name)
        ).scalar_one_or_none()
        if self._template is None:
            self._template = AttributeGroupTemplate(name=group_name)
            if isinstance(self.parent._template, ResourceTemplate):
                self._template.resource_templates.append(self.parent._template)
            elif isinstance(self.parent._template, StepTemplate):
                self._template.step_templates.append(self.parent._template)
            self.session.add(self._template)
            self.session.flush()

    def add_attribute(
        self, attr_name: str, value_type: str, unit: str, default: Any
    ) -> "AttributeGroupBuilder":
        attribute = self.session.execute(
            select(AttributeTemplate).filter_by(
                name=attr_name, value_type=value_type, attribute_template=self._template
            )
        ).scalar_one_or_none()
        if attribute is None:
            attribute = AttributeTemplate(
                name=attr_name,
                value_type=value_type,
                attribute_template=self._template,
                default_value=default,
                unit=unit,
            )
            self.session.add(attribute)
            self.session.flush()
        return self

    def remove_attribute(self, attr_name: str) -> "AttributeGroupBuilder":
        q = (
            select(AttributeGroupTemplate)
            .filter_by(name=self.group_name)
            .where(
                with_parent(
                    self.parent._template,
                    self.parent._template.__class__.attribute_group_templates,
                )
            )
        )
        attr_group = self.session.execute(q).scalar_one_or_none()

        if attr_group is None:
            warnings.warn(
                f"Property group does not exist : {self.group_name}", stacklevel=2
            )
            return self

        attribute = self.session.execute(
            select(AttributeTemplate).filter_by(
                name=attr_name, attribute_template=attr_group
            )
        ).scalar_one_or_none()
        if attribute is None:
            warnings.warn(
                f"Property does not exist in group {self.group_name}: {attr_name}",
                stacklevel=2,
            )
            return self

        attr_group.value_templates.remove(attribute)
        return self

    def close_group(self):
        if self.parent:
            return self.parent
