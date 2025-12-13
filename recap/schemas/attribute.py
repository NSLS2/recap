from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from recap.schemas.common import CommonFields
from recap.utils.general import CONVERTERS

TypeName = Literal["int", "float", "bool", "str", "datetime", "array", "enum"]


class AttributeEnumOptionSchema(CommonFields):
    value: str
    label: str | None = None
    payload: dict[str, Any] | None = None


class AttributeTemplateSchema(CommonFields):
    name: str
    slug: str
    value_type: TypeName
    unit: str | None
    default_value: Any
    enum_options: list[AttributeEnumOptionSchema] = Field(default_factory=list)


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
    options: list[str] = Field(default_factory=list)
    default: Any = Field(default=None)

    @field_validator("options", mode="before")
    @classmethod
    def normalize_options(cls, v):
        if not v:
            return []

        normalized = []
        for opt in v:
            if opt is None:
                continue
            if isinstance(opt, str):
                normalized.append(opt)
                continue
            if isinstance(opt, dict):
                val = opt.get("value")
            else:
                val = getattr(opt, "value", None)
            if val is None:
                raise ValueError("Enum options must provide a 'value'")
            normalized.append(str(val))

        # Preserve insertion order while removing duplicates
        seen = set()
        deduped = []
        for val in normalized:
            if val in seen:
                continue
            seen.add(val)
            deduped.append(val)
        return deduped

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
            converted = conv(v)
        except Exception as e:
            raise ValueError(f"`default` not coercible to {t}: {e}") from e
        options = info.data.get("options") or []
        if (
            t == "enum"
            and converted is not None
            and options
            and (str(converted) not in options)
        ):
            raise ValueError(
                f"`default` value {converted!r} not in allowed options: "
                f"{', '.join(options)}"
            )
        return converted
