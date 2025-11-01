from typing import Annotated, Any, Self

from pydantic import Field, SkipValidation

from recap.schemas.attribute import (
    AttributeGroupTemplateSchema,
    AttributeTemplateSchema,
)
from recap.schemas.common import CommonFields
from recap.schemas.resource import ResourceSlotSchema, ResourceTypeSchema
from recap.schemas.step import StepSchema, StepTemplateSchema


class CampaignSchema(CommonFields):
    name: str
    proposal: str
    saf: str | None
    meta_data: dict[str, Any] | None


class ProcessTemplateSchema(CommonFields):
    name: str
    version: str
    is_active: bool
    step_templates: list[StepTemplateSchema]
    resource_slots: list[ResourceSlotSchema]


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
    values: dict[str, AttributeTemplateSchema]


class ResourceSchema(CommonFields):
    name: str
    template: ResourceTemplateSchema
    parent: "ResourceSchema | None"
    children: list["ResourceSchema"]
    properties: list[PropertySchema]


class ProcessRunSchema(CommonFields):
    name: str
    description: str
    campaign: CampaignSchema
    template: ProcessTemplateSchema
    steps: list[StepSchema]
    resources: dict[str, ResourceSchema]
    assignments: dict[ResourceSlotSchema, ResourceSchema]
