from recap.schemas.common import CommonFields


class AttributeTemplateSchema(CommonFields):
    name: str
    slug: str
    value_type: str
    unit: str
    default_value: str | None


class AttributeGroupTemplateSchema(CommonFields):
    name: str
    slug: str
    attribute_templates: list[AttributeTemplateSchema]
