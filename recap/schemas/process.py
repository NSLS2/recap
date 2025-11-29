from typing import Annotated, Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SkipValidation, model_validator

from recap.schemas.attribute import (
    AttributeGroupTemplateSchema,
    AttributeTemplateValidator,
)
from recap.schemas.common import CommonFields
from recap.schemas.resource import ResourceSlotSchema, ResourceTypeSchema
from recap.schemas.step import StepSchema, StepTemplateSchema


class CampaignSchema(CommonFields):
    name: str
    proposal: str
    saf: str | None
    meta_data: dict[str, Any] | None
    process_runs: list["ProcessRunSchema"]


class ProcessTemplateRef(CommonFields):
    name: str
    version: str


class ProcessTemplateSchema(CommonFields):
    name: str
    version: str
    is_active: bool
    step_templates: list[StepTemplateSchema]
    resource_slots: list[ResourceSlotSchema]


class ResourceTemplateRef(CommonFields):
    name: str
    slug: str | None
    parent: Self | None = Field(default=None, exclude=True)
    types: list[ResourceTypeSchema] = Field(default_factory=list)


class ResourceTemplateSchema(CommonFields):
    name: str
    slug: "str | None"
    types: list[ResourceTypeSchema] = Field(default_factory=list)
    parent: Annotated[Self | None, SkipValidation] = Field(default=None, exclude=True)
    children: list[Self] = Field(default_factory=list)
    attribute_group_templates: list[AttributeGroupTemplateSchema]


ResourceTypeSchema.model_rebuild()


class PropertySchema(CommonFields):
    template: AttributeGroupTemplateSchema
    values: dict[str, Any]

    @model_validator(mode="after")
    def validate_and_coerce_values(self) -> "PropertySchema":
        # Build template lookup: attr name -> template schema
        tmpl_by_name = {a.name: a for a in self.template.attribute_templates}

        # 1) no unknown keys
        unknown_keys = set(self.values) - set(tmpl_by_name)
        if unknown_keys:
            raise ValueError(
                f"Unknown parameter(s) for template {self.template.name}: "
                f"{', '.join(sorted(unknown_keys))}"
            )

        # 2) coerce each value using your AttributeTemplateValidator
        coerced: dict[str, Any] = {}
        for name, raw_value in self.values.items():
            attr_tmpl = tmpl_by_name[name]

            # Reuse your validator to perform type coercion & checks
            # Note: we shove `raw_value` into 'default' to leverage coerce_default()
            validator = AttributeTemplateValidator(
                name=attr_tmpl.name,
                type=attr_tmpl.value_type,
                unit=attr_tmpl.unit,
                default=raw_value,
            )
            coerced[name] = validator.default  # already converted by coerce_default

        self.values = coerced
        return self


class ResourceRef(CommonFields):
    name: str
    template: ResourceTemplateRef


class ResourceSchema(CommonFields):
    name: str
    template: ResourceTemplateSchema
    parent: "ResourceSchema | None"
    children: list["ResourceSchema"]
    properties: dict[str, PropertySchema]


class ResourceAssignmentSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
    slot: ResourceSlotSchema
    resource: ResourceSchema


class ProcessRunSchema(CommonFields):
    name: str
    description: str
    # campaign: CampaignSchema
    campaign_id: UUID
    template: ProcessTemplateSchema
    steps: list[StepSchema]
    # resources: dict[ResourceSlotSchema, ResourceSchema]
    assigned_resources: list[ResourceAssignmentSchema]
