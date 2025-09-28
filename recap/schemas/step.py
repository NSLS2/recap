from recap.schemas.attribute import (
    AttributeGroupTemplateSchema,
    AttributeTemplateSchema,
)
from recap.schemas.common import CommonFields, StepStatus
from recap.schemas.resource import ResourceSlotSchema


class StepTemplateSchema(CommonFields):
    name: str
    attribute_group_templates: list[AttributeGroupTemplateSchema]
    resource_slots: dict[str, ResourceSlotSchema]


class ParameterSchema(CommonFields):
    template: AttributeGroupTemplateSchema
    value: dict[str, AttributeTemplateSchema]


class StepSchema(CommonFields):
    name: str
    template: StepTemplateSchema
    parameters: dict[str, ParameterSchema]
    state: StepStatus
