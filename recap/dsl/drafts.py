from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from recap.schemas.attribute import AttributeTemplateValidator, TypeName
from recap.utils.dsl import ReadOnlyMixin, read_only_view
from recap.utils.general import Direction


class DraftModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def draft_copy(model: BaseModel) -> BaseModel:
    """Detach owned state while exposing external canonical references read-only.

    Owned objects take precedence over shared references, preserving back-links
    such as a copied child's parent pointing to the copied root.
    """
    if isinstance(model, ReadOnlyMixin):
        return model
    memo = _draft_copy_memo(model)
    # Pydantic's hook registers the model only after copying its fields. Seed
    # the root first so child-to-root back-links cannot create a second root.
    result = object.__new__(type(model))
    memo[id(model)] = result
    copied = model.__deepcopy__(memo)
    for name in (
        "__dict__",
        "__pydantic_extra__",
        "__pydantic_fields_set__",
        "__pydantic_private__",
    ):
        object.__setattr__(result, name, object.__getattribute__(copied, name))
    return result


def _draft_copy_memo(model: BaseModel) -> dict[int, BaseModel]:
    owned = set()
    shared = {}
    visited = set()

    def visit(value, *, external=False):
        if isinstance(value, ReadOnlyMixin):
            return
        if isinstance(value, BaseModel):
            if external:
                shared[id(value)] = value
                return
            if id(value) in owned:
                return
            owned.add(id(value))
            boundary = getattr(type(value), "_draft_shared_fields", frozenset())
            for name in type(value).model_fields:
                visit(value.__dict__.get(name), external=name in boundary)
        elif isinstance(value, (dict, list, tuple, set)):
            key = (id(value), external)
            if key in visited:
                return
            visited.add(key)
            for item in value.values() if isinstance(value, dict) else value:
                visit(item, external=external)

    visit(model)
    return {
        key: read_only_view(value) for key, value in shared.items() if key not in owned
    }


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
    id: UUID | None = None
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


class ResourceTemplateDraft(DraftModel):
    id: UUID | None = None
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    labels: tuple[str, ...] = ()
    type_names: tuple[str, ...] = ()
    property_groups: tuple[AttributeGroupDraft, ...] = ()
    children: tuple[ResourceTemplateDraft, ...] = ()

    @model_validator(mode="after")
    def validate_nested_names(self):
        _require_unique(self.property_groups, "property group")
        _require_unique(self.children, "child template")
        return self


class ProcessRunStepDraft(DraftModel):
    parameters: dict[str, dict[str, Any]] = Field(default_factory=dict)
    resources: dict[str, UUID] = Field(default_factory=dict)


class ChildStepDraft(DraftModel):
    id: UUID
    parent_id: UUID
    template_id: UUID
    name: str = Field(min_length=1)
    parameters: dict[str, dict[str, Any]] = Field(default_factory=dict)
    resources: dict[str, UUID] = Field(default_factory=dict)


class ProcessRunDraft(DraftModel):
    id: UUID | None = None
    name: str = Field(min_length=1)
    description: str
    template_id: UUID
    assignments: dict[str, UUID] = Field(default_factory=dict)
    steps: dict[str, ProcessRunStepDraft] = Field(default_factory=dict)
    child_steps: tuple[ChildStepDraft, ...] = ()


def _require_unique(items, kind: str) -> None:
    seen: set[str] = set()
    for item in items:
        if item.name in seen:
            raise ValueError(f"Duplicate {kind} name: {item.name!r}")
        seen.add(item.name)
