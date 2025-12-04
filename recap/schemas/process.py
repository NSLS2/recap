from typing import Any
from uuid import UUID

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
    step_templates: list[StepTemplateSchema]
    resource_slots: list["ResourceSlotSchema"]


class ProcessRunSchema(CommonFields):
    name: str
    description: str
    # campaign: CampaignSchema
    campaign_id: UUID
    template: ProcessTemplateSchema
    steps: list[StepSchema]
    # resources: dict[ResourceSlotSchema, ResourceSchema]
    assigned_resources: list[ResourceAssignmentSchema]


class CampaignSchema(CommonFields):
    name: str
    proposal: str
    saf: str | None
    meta_data: dict[str, Any] | None
    process_runs: list["ProcessRunSchema"]
