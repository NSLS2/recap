from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from recap.schemas.common import CommonFields
from recap.utils.general import CONVERTERS

TypeName = Literal["int", "float", "bool", "str", "datetime", "array", "enum"]


class AttributeTemplateSchema(CommonFields):
    name: str
    slug: str
    value_type: TypeName
    unit: str | None
    default_value: Any
    metadata: dict[str, Any] | None = Field(default_factory=dict, alias="metadata_json")


class AttributeGroupRef(CommonFields):
    name: str
    slug: str


class AttributeGroupTemplateSchema(CommonFields):
    name: str
    slug: str
    attribute_templates: list[AttributeTemplateSchema]


class AttributeTemplateValidator(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    name: str
    type: TypeName
    unit: str | None = ""
    default: Any = Field(default=None)
    metadata: dict[str, Any] | None = Field(default_factory=dict)

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
            coerced = conv(v)
        except Exception as e:
            raise ValueError(f"`default` not coercible to {t}: {e}") from e
        if t == "enum":
            coerced = str(coerced)
        return coerced

    @model_validator(mode="after")
    def enforce_enum_choices(self) -> "AttributeTemplateValidator":
        if self.type != "enum":
            return self

        choices = (self.metadata or {}).get("choices")
        if not choices:
            raise ValueError("enum attributes require metadata.choices to be set")
        if self.default is not None and str(self.default) not in choices:
            raise ValueError(
                f"default must be one of {', '.join(choices)} (got {self.default})"
            )
        return self
