from pydantic import BaseModel

from recap.schemas.common import Attribute


class SubcontainerLevel(BaseModel):
    level_name: str
    count: int
    alphabetical_name: bool
    uppercase_name: bool
    zero_padding: int
    prefix: str
    suffix: str


class SubcontainerGeneration(BaseModel):
    levels: list[SubcontainerLevel]
    naming_pattern: str


class ContainerTypeSchema(BaseModel):
    name: str
    ref_name: str
    attributes: list[Attribute] | None = None
    subcontainer_generation: SubcontainerGeneration | None = None
    subcontainer_attributes: list[Attribute] | None = None


class ContainerSchema(BaseModel):
    name: str
    ref_name: str

    ref_name: str
