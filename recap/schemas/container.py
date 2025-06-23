from typing import List, Optional

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
    levels: List[SubcontainerLevel]
    naming_pattern: str


class ContainerTypeSchema(BaseModel):
    name: str
    ref_name: str
    attributes: Optional[List[Attribute]] = None
    subcontainer_generation: Optional[SubcontainerGeneration] = None
    subcontainer_attributes: Optional[List[Attribute]] = None


class ContainerSchema(BaseModel):
    name: str
    ref_name: str

    ref_name: str
