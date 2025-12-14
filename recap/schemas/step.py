from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from recap.db.step import Parameter
from recap.schemas.attribute import (
    AttributeGroupTemplateSchema,
    AttributeTemplateValidator,
)
from recap.schemas.common import CommonFields, StepStatus
from recap.schemas.resource import ResourceSchema, ResourceSlotSchema
from recap.utils.dsl import AliasMixin, build_param_values_model


def _attr_metadata(vt: Any) -> dict | None:
    meta = getattr(vt, "metadata", None)
    meta_json = getattr(vt, "metadata_json", None)
    if isinstance(meta, dict):
        return meta
    elif isinstance(meta_json, dict):
        return meta_json
    else:
        return {}


class StepTemplateRef(CommonFields):
    name: str


class StepTemplateSchema(CommonFields):
    name: str
    attribute_group_templates: list[AttributeGroupTemplateSchema]
    resource_slots: dict[str, ResourceSlotSchema]


class ParameterSchema(CommonFields):
    template: AttributeGroupTemplateSchema
    # values: dict[str, AttributeTemplateSchema]
    values: BaseModel  # dict[str, Any]

    @model_validator(mode="before")
    def coerce_from_orm(cls, data):
        if isinstance(data, Parameter):
            tmpl = data.template
            tmpl_key = tuple(
                (
                    vt.name,
                    vt.slug,
                    vt.value_type,
                    _attr_metadata(vt),
                )
                for vt in tmpl.attribute_templates
            )
            values_model = build_param_values_model(tmpl.slug or tmpl.name, tmpl_key)
            raw_values = {av.template.name: av.value for av in data._values.values()}
            return {
                "id": data.id,
                "create_date": data.create_date,
                "modified_date": data.modified_date,
                "template": tmpl,
                "values": values_model.model_validate(raw_values),
            }
        if isinstance(data, dict) and isinstance(data.get("values"), dict):
            tmpl = data.get("template")
            if tmpl:
                tmpl_key = tuple(
                    (
                        vt.name,
                        vt.slug,
                        vt.value_type,
                        _attr_metadata(vt),
                    )
                    for vt in tmpl.attribute_templates
                )
                values_model = build_param_values_model(
                    tmpl.slug or tmpl.name, tmpl_key
                )
                data["values"] = values_model.model_validate(data["values"])
        return data

    @model_validator(mode="before")
    def coerce_and_reject_unknown(cls, data):
        if not isinstance(data, dict):
            return data
        template = data.get("template")
        raw_values = data.get("values") or {}
        if template and isinstance(raw_values, dict):
            tmpl_names = {a.name for a in template.attribute_templates}
            unknown = set(raw_values) - tmpl_names
            if unknown:
                raise ValueError(
                    f"Unknown parameter(s) for template {template.name}: "
                    f"{', '.join(sorted(unknown))}"
                )
            tmpl_key = tuple(
                (
                    vt.name,
                    vt.slug,
                    vt.value_type,
                    _attr_metadata(vt),
                )
                for vt in template.attribute_templates
            )
            values_model = build_param_values_model(
                template.slug or template.name, tmpl_key
            )
            data["values"] = values_model.model_validate(raw_values)
        return data

    @model_validator(mode="after")
    def validate_and_coerce_values(self) -> "ParameterSchema":
        # Build template lookup: attr name -> template schema
        tmpl_by_name = {a.name: a for a in self.template.attribute_templates}

        values_dict = (
            self.values.model_dump(by_alias=True)
            if isinstance(self.values, BaseModel)
            else dict(self.values)
        )

        # 1) no unknown keys
        unknown_keys = set(values_dict) - set(tmpl_by_name)
        if unknown_keys:
            raise ValueError(
                f"Unknown parameter(s) for template {self.template.name}: "
                f"{', '.join(sorted(unknown_keys))}"
            )

        # 2) coerce each value using your AttributeTemplateValidator
        coerced: dict[str, Any] = {}
        for name, raw_value in values_dict.items():
            attr_tmpl = tmpl_by_name[name]

            # Reuse your validator to perform type coercion & checks
            # Note: we shove `raw_value` into 'default' to leverage coerce_default()
            validator = AttributeTemplateValidator(
                name=attr_tmpl.name,
                type=attr_tmpl.value_type,
                unit=attr_tmpl.unit,
                metadata=_attr_metadata(attr_tmpl),
                default=raw_value,
            )
            coerced[name] = validator.default  # already converted by coerce_default

        self.values = self.values.__class__.model_validate(coerced)
        return self


class StepSchema(CommonFields):
    name: str
    template: StepTemplateSchema
    parameters: BaseModel | dict[str, ParameterSchema]
    state: StepStatus
    process_run_id: UUID
    parent_id: UUID | None = None
    children: list["StepSchema"] = Field(default_factory=list)
    resources: dict[str, "ResourceSchema"] = Field(default_factory=dict)

    def generate_child(self):
        return self.model_copy(deep=True, update={"id": None, "parent_id": self.id})

    @model_validator(mode="after")
    def build_parameter_model(self) -> "StepSchema":
        if isinstance(self.parameters, BaseModel):
            return self

        param_fields: dict[str, tuple] = {}
        param_values: dict[str, ParameterSchema] = {}
        for param in self.parameters.values():
            tmpl = param.template
            field_name = getattr(tmpl, "slug", None) or tmpl.name
            param_fields[field_name] = (ParameterSchema, Field(alias=tmpl.name))
            param_values[field_name] = param

        if param_fields:
            model = create_model(
                f"StepParameters_{self.template.name}",
                __base__=(AliasMixin, BaseModel),
                __config__=ConfigDict(
                    validate_assignment=True,
                    populate_by_name=True,
                    arbitrary_types_allowed=True,
                ),
                **param_fields,
            )
            self.parameters = model.model_validate(param_values)

        return self


StepSchema.model_rebuild()
