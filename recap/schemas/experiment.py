from typing import List, Optional

from pydantic import BaseModel, Field

from recap.schemas.common import Attribute
from recap.schemas.container import ContainerSchema, ContainerTypeSchema

decomposable_description = """
A decomposable action that is repeated over sub containers
in source_container and dest_container.
For e.g. Transfer liquid from one plate to another.
This can be decomposed into transfer liquid from each well in source plate to destination plate
"""

custom_subactions_description = """
  Some actions are easy to decompose. i.e if transferring liquid from a beaker to a 96 well plate.
  But some are not, for example transferring liquid from multiple wells in the source plate to a
  single well in the destination plate. These transfers can only be defined when the experiment is
  created
"""


class ActionTypeSchema(BaseModel):
    name: str = Field(..., description="Name of the action type")
    attributes: Optional[List[Attribute]] = None
    action_types: Optional["ActionTypeSchema"] = None
    source_container: Optional[ContainerTypeSchema] = None
    dest_container: ContainerTypeSchema
    decomposable: bool = Field(default=False, description=decomposable_description)
    custom_subactions: bool = Field(default=False, description=custom_subactions_description)


class ActionSchema(BaseModel):
    name: str = Field(..., description="Action name")
    action_type: ActionTypeSchema
    source_container: Optional[ContainerSchema] = None
    dest_container: ContainerSchema
    subactions: Optional[List["ActionSchema"]] = None


class WorkflowTypeSchema(BaseModel):
    name: str = Field(..., description="Name of the experiment type")
    action_types: List[ActionTypeSchema] = Field(..., description="List of ordered action steps")


class WorkflowSchema(BaseModel):
    name: str = Field(..., description="Name of the experiment")
    actions: List[ActionSchema] = Field(..., description="Instance of an action")


class WorkflowData(BaseModel):
    container_types: Optional[List[ContainerTypeSchema]] = None
    experiment_type: Optional[WorkflowTypeSchema] = None
