from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from recap.schemas.attribute import AttributeTemplateValidator, TypeName
from recap.utils.general import Direction


class DraftModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AttributeDraft(DraftModel):
    name: str = Field(min_length=1)
    type: TypeName
    unit: str = ""
    default: Any = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_definition(self):
        AttributeTemplateValidator.model_validate(self.model_dump())
        return self


class AttributeGroupDraft(DraftModel):
    name: str = Field(min_length=1)
    attributes: tuple[AttributeDraft, ...] = ()

    @model_validator(mode="after")
    def validate_attribute_names(self):
        _require_unique(self.attributes, "attribute")
        return self


class ResourceSlotDraft(DraftModel):
    name: str = Field(min_length=1)
    resource_type: str = Field(min_length=1)
    direction: Direction
    required: bool = True
    create_resource_type: bool = False


class StepTemplateDraft(DraftModel):
    name: str = Field(min_length=1)
    parameter_groups: tuple[AttributeGroupDraft, ...] = ()
    role_bindings: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_group_names(self):
        _require_unique(self.parameter_groups, "parameter group")
        if any(not role or not slot for role, slot in self.role_bindings.items()):
            raise ValueError("Role and resource slot names must not be empty")
        return self


class ProcessTemplateDraft(DraftModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    labels: tuple[str, ...] = ()
    resource_slots: tuple[ResourceSlotDraft, ...] = ()
    steps: tuple[StepTemplateDraft, ...] = ()

    @model_validator(mode="after")
    def validate_nested_names_and_references(self):
        _require_unique(self.resource_slots, "resource slot")
        _require_unique(self.steps, "step")
        slot_names = {slot.name for slot in self.resource_slots}
        for step in self.steps:
            for role, slot_name in step.role_bindings.items():
                if slot_name not in slot_names:
                    raise ValueError(
                        f"Role {role!r} on step {step.name!r} references unknown "
                        f"resource slot {slot_name!r}"
                    )
        return self


def _require_unique(items, kind: str) -> None:
    seen: set[str] = set()
    for item in items:
        if item.name in seen:
            raise ValueError(f"Duplicate {kind} name: {item.name!r}")
        seen.add(item.name)
