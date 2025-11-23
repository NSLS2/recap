import typing
from typing import Any, Generic, TypeVar

from recap.adapter import Backend

if typing.TYPE_CHECKING:
    from recap.dsl.process_builder import StepTemplateBuilder
    from recap.dsl.resource_builder import ResourceTemplateBuilder

ParentType = TypeVar(
    "ParentType", bound="ResourceTemplateBuilder | StepTemplateBuilder"
)


class AttributeGroupBuilder(Generic[ParentType]):
    def __init__(
        self,
        group_name: str,
        parent: ParentType,
    ):
        self.group_name = group_name
        self.parent: ParentType = parent
        self.backend: Backend = parent.backend
        self._attribute_group = self.backend.add_attr_group(
            self.group_name, self.parent._template
        )

    def add_attribute(
        self, attr_name: str, value_type: str, unit: str, default: Any
    ) -> "AttributeGroupBuilder[ParentType]":
        self.backend.add_attribute(
            attr_name, value_type, unit, default, self._attribute_group
        )
        return self

    def remove_attribute(self, attr_name: str) -> "AttributeGroupBuilder":
        self.backend.remove_attribute(attr_name)
        return self

    def close_group(self) -> ParentType:
        return self.parent
