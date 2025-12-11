from typing import Any
from uuid import UUID

from pydantic import ConfigDict

from recap.schemas.common import CommonFields
from recap.schemas.resource import ResourceAssignmentSchema, ResourceSlotSchema
from recap.schemas.step import StepSchema, StepTemplateSchema


class ProcessTemplateRef(CommonFields):
    name: str
    version: str


class ProcessTemplateSchema(CommonFields):
    name: str
    version: str
    is_active: bool
    step_templates: dict[str, StepTemplateSchema]
    resource_slots: list["ResourceSlotSchema"]


class ProcessRunSchema(CommonFields):
    name: str
    description: str
    campaign_id: UUID
    template: ProcessTemplateSchema
    steps: dict[str, StepSchema]
    assigned_resources: list[ResourceAssignmentSchema]
    model_config = ConfigDict(arbitrary_types_allowed=True, from_attributes=True)


class CampaignSchema(CommonFields):
    name: str
    proposal: str
    saf: str | None
    meta_data: dict[str, Any] | None
    process_runs: list["ProcessRunSchema"]
