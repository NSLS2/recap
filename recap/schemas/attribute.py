from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from recap.schemas.common import CommonFields
from recap.utils.general import CONVERTERS


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


TypeName = Literal["int", "float", "bool", "str", "datetime", "array"]


class AttributeValidator(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    name: str
    type: TypeName
    unit: str = ""
    default: Any = Field(default=None)

    @field_validator("default")
    @classmethod
    def coerce_default(cls, v: Any, info: ValidationInfo) -> Any:
        t = info.data.get("type")
        if t is None:
            raise ValueError("`type` must be provided before `default`")
        conv = CONVERTERS.get(t)
        if conv is None:
            raise ValueError(f"Unsupported type: {t!r}")
        try:
            return conv(v)
        except Exception as e:
            raise ValueError(f"`default` not coercible to {t}: {e}") from e
