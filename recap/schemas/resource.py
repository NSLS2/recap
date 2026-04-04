from typing import Any

try:
    from typing import Self
except ImportError:  # Python <3.11
    from typing_extensions import Self  # noqa

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from recap.db.resource import Property
from recap.schemas.attribute import (
    AttributeGroupTemplateSchema,
    AttributeTemplateValidator,
    AttributeValueSchema,
)
from recap.schemas.common import CommonFields
from recap.utils.dsl import AliasMixin, build_param_values_model
from recap.utils.general import Direction


def _attr_metadata(vt: Any) -> dict | None:
    meta = getattr(vt, "metadata", None)
    meta_json = getattr(vt, "metadata_json", None)
    if isinstance(meta, dict):
        return meta
    elif isinstance(meta_json, dict):
        return meta_json
    else:
        return {}


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
                    vt.unit,
                )
                for vt in tmpl.attribute_templates
            )
            values_model = build_param_values_model(tmpl.slug or tmpl.name, tmpl_key)
            raw_values = {
                av.template.name: {"value": av.value, "unit": av.unit}
                for av in data._values.values()
            }
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
                    (vt.name, vt.slug, vt.value_type, _attr_metadata(vt), vt.unit)
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
            if isinstance(raw_value, dict):
                raw_unit = raw_value.get("unit")
                raw_value = raw_value.get("value")
            else:
                raw_unit = None

            validator = AttributeTemplateValidator(
                name=attr_tmpl.name,
                type=attr_tmpl.value_type,
                unit=attr_tmpl.unit,
                metadata=_attr_metadata(attr_tmpl),
                default=raw_value,
            )
            coerced[name] = {
                "value": validator.default,
                "unit": attr_tmpl.unit if raw_unit is None else raw_unit,
            }

        self.values = self.values.__class__.model_validate(coerced)
        return self

    def __getattr__(self, name: str) -> Any:
        # Only called when normal attribute resolution fails (i.e. `name` is not
        # a real field on PropertySchema such as `template`, `values`, `id`, etc.)
        # This allows `prop.smiles` as a shortcut for `prop.values.smiles`.
        # Note: if an attribute group defines a field that clashes with a
        # PropertySchema field name, the shortcut won't reach it.
        values = self.__dict__.get("values")
        if values is not None:
            try:
                return getattr(values, name)
            except AttributeError:
                pass
        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )

    def __setattr__(self, name: str, value: Any) -> None:
        # Pydantic internals and real model fields are handled normally.
        # Unknown names are forwarded to values, mutating .value in-place
        # on the AttributeValueSchema so that the unit is preserved.
        if name.startswith("__") or name in type(self).model_fields:
            super().__setattr__(name, value)
            return
        values = self.__dict__.get("values")
        if values is not None and name in type(values).model_fields:
            attr_schema = getattr(values, name)
            if isinstance(attr_schema, AttributeValueSchema):
                attr_schema.value = value
                return
            setattr(values, name, value)
            return
        super().__setattr__(name, value)


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
    parent: ResourceTemplateRef | None = Field(default=None, exclude=True)
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
    parent: "ResourceRef | None" = Field(default=None, exclude=True)
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
