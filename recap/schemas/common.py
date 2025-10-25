from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator


class ValueType(str, Enum):
    INT = "int"
    STR = "str"
    BOOL = "bool"
    FLOAT = "float"


class StepStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"


# Mapping from ValueType enum to the Python type we expect
TYPE_MAP = {
    ValueType.INT: int,
    ValueType.STR: str,
    ValueType.BOOL: bool,
    ValueType.FLOAT: float,
}

# DefaultValue = Union[int, float, bool, str]
DefaultValue = int | float | bool | str


class Attribute(BaseModel):
    name: str
    slug: str
    value_type: ValueType
    default_value: DefaultValue

    @model_validator(mode="after")
    def check_default_value(self):
        if not isinstance(self.default_value, TYPE_MAP[self.value_type]):
            raise ValueError(
                f"default_value must be {TYPE_MAP[self.value_type].__name__}",
                f"got {type(self.default_value).__name__} instead.",
            )
        return self


class CommonFields(BaseModel):
    id: UUID
    create_date: datetime
    modified_date: datetime

    model_config = ConfigDict(from_attributes=True)
