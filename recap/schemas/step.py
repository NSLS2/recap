from typing import Any
from uuid import UUID

from pydantic import Field, model_validator

from recap.schemas.attribute import (
    AttributeGroupTemplateSchema,
    AttributeTemplateValidator,
)
from recap.schemas.common import CommonFields, StepStatus
from recap.schemas.resource import ResourceSchema, ResourceSlotSchema


class StepTemplateRef(CommonFields):
    name: str


class StepTemplateSchema(CommonFields):
    name: str
    attribute_group_templates: list[AttributeGroupTemplateSchema]
    resource_slots: dict[str, ResourceSlotSchema]


class ParameterSchema(CommonFields):
    template: AttributeGroupTemplateSchema
    # values: dict[str, AttributeTemplateSchema]
    values: dict[str, Any]

    @model_validator(mode="after")
    def validate_and_coerce_values(self) -> "ParameterSchema":
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


class StepSchema(CommonFields):
    name: str
    template: StepTemplateSchema
    parameters: dict[str, ParameterSchema]
    state: StepStatus
    process_run_id: UUID
    parent_id: UUID | None = None
    children: list["StepSchema"] = Field(default_factory=list)
    resources: dict[str, "ResourceSchema"] = Field(default_factory=dict)


StepSchema.model_rebuild()
