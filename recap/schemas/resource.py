from typing import Annotated, Any, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SkipValidation,
    create_model,
    model_validator,
)

from recap.db.process import Direction
from recap.db.resource import Property
from recap.schemas.attribute import (
    AttributeGroupTemplateSchema,
    AttributeTemplateValidator,
)
from recap.schemas.common import CommonFields
from recap.utils.dsl import AliasMixin, build_param_values_model


def _attr_metadata(vt: Any) -> dict | None:
    meta = getattr(vt, "metadata_json", None)
    if meta is None:
        maybe = getattr(vt, "metadata", None)
        if isinstance(maybe, dict):
            meta = maybe
    return meta


class PropertySchema(CommonFields):
    template: AttributeGroupTemplateSchema
    values: BaseModel

    @model_validator(mode="before")
    def coerce_from_orm_or_dict(cls, data):
        if isinstance(data, Property):
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
                tmpl_names = {a.name for a in tmpl.attribute_templates}
                unknown = set(data["values"]) - tmpl_names
                if unknown:
                    raise ValueError(
                        f"Unknown property(s) for template {tmpl.name}: "
                        f"{', '.join(sorted(unknown))}"
                    )
                tmpl_key = tuple(
                    (vt.name, vt.slug, vt.value_type, _attr_metadata(vt))
                    for vt in tmpl.attribute_templates
                )
                values_model = build_param_values_model(
                    tmpl.slug or tmpl.name, tmpl_key
                )
                data["values"] = values_model.model_validate(data["values"])
        return data

    @model_validator(mode="after")
    def validate_and_coerce_values(self) -> "PropertySchema":
        tmpl_by_name = {a.name: a for a in self.template.attribute_templates}

        values_dict = (
            self.values.model_dump(by_alias=True)
            if isinstance(self.values, BaseModel)
            else dict(self.values)
        )

        unknown_keys = set(values_dict) - set(tmpl_by_name)
        if unknown_keys:
            raise ValueError(
                f"Unknown property(s) for template {self.template.name}: "
                f"{', '.join(sorted(unknown_keys))}"
            )

        coerced: dict[str, Any] = {}
        for name, raw_value in values_dict.items():
            attr_tmpl = tmpl_by_name[name]

            validator = AttributeTemplateValidator(
                name=attr_tmpl.name,
                type=attr_tmpl.value_type,
                unit=attr_tmpl.unit,
                metadata=_attr_metadata(attr_tmpl),
                default=raw_value,
            )
            coerced[name] = validator.default

        self.values = self.values.__class__.model_validate(coerced)
        return self


class ResourceTypeSchema(CommonFields):
    name: str


class ResourceTemplateRef(CommonFields):
    name: str
    slug: str | None
    version: str
    parent: Self | None = Field(default=None, exclude=True)
    types: list[ResourceTypeSchema] = Field(default_factory=list)


class ResourceTemplateSchema(CommonFields):
    name: str
    slug: "str | None"
    version: str
    types: list[ResourceTypeSchema] = Field(default_factory=list)
    parent: Annotated[Self | None, SkipValidation] = Field(default=None, exclude=True)
    children: dict[str, Self] = Field(default_factory=dict)
    attribute_group_templates: list[AttributeGroupTemplateSchema]


ResourceTypeSchema.model_rebuild()


class ResourceSlotSchema(CommonFields):
    name: str
    resource_type: ResourceTypeSchema
    direction: Direction


class ResourceSchema(CommonFields):
    name: str
    template: ResourceTemplateSchema
    parent: Annotated[Self | None, SkipValidation] = Field(default=None, exclude=True)
    children: dict[str, Self]
    properties: BaseModel | dict[str, PropertySchema]
    model_config = ConfigDict(arbitrary_types_allowed=True, from_attributes=True)

    @model_validator(mode="after")
    def build_property_model(self) -> "ResourceSchema":
        if isinstance(self.properties, BaseModel):
            return self

        prop_fields: dict[str, tuple] = {}
        prop_values: dict[str, PropertySchema] = {}
        for prop in self.properties.values():
            tmpl = prop.template
            field_name = getattr(tmpl, "slug", None) or tmpl.name
            prop_fields[field_name] = (PropertySchema, Field(alias=tmpl.name))
            prop_values[field_name] = prop

        if prop_fields:
            model = create_model(
                f"ResourceProperties_{self.template.slug or self.template.name}",
                __base__=(AliasMixin, BaseModel),
                __config__=ConfigDict(
                    validate_assignment=True,
                    populate_by_name=True,
                    arbitrary_types_allowed=True,
                ),
                **prop_fields,
            )
            self.properties = model.model_validate(prop_values)

        return self


class ResourceRef(CommonFields):
    name: str
    template: ResourceTemplateRef


class ResourceAssignmentSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
    slot: ResourceSlotSchema
    resource: ResourceSchema
    step_id: UUID | None = None
