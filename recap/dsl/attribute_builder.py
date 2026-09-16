import typing
from typing import Any, Generic, TypeVar

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
        raise RuntimeError(
            "AttributeGroupBuilder direct mutation is unsupported; use a "
            "command-backed template builder"
        )

    def add_attribute(
        self,
        attr_name: str,
        value_type: str,
        unit: str,
        default: Any,
        metadata: dict[str, Any] | None = None,
    ) -> "AttributeGroupBuilder[ParentType]":
        raise RuntimeError("AttributeGroupBuilder direct mutation is unsupported")

    def remove_attribute(self, attr_name: str) -> "AttributeGroupBuilder":
        raise RuntimeError("AttributeGroupBuilder direct mutation is unsupported")

    def close_group(self) -> ParentType:
        return self.parent
