"""Strawberry GraphQL types derived from recap Pydantic schemas."""
from __future__ import annotations

from datetime import datetime

import strawberry
from strawberry.scalars import JSON


@strawberry.type
class AttributeValueType:
    id: strawberry.ID
    name: str
    slug: str
    value: JSON | None
    create_date: datetime
    modified_date: datetime


@strawberry.type
class AttributeGroupType:
    id: strawberry.ID
    name: str
    attributes: list[AttributeValueType]


@strawberry.type
class ResourceTemplateType:
    id: strawberry.ID
    name: str
    version: str
    create_date: datetime
    modified_date: datetime


@strawberry.type
class ResourceType:
    id: strawberry.ID
    name: str
    create_date: datetime
    modified_date: datetime
    template: ResourceTemplateType | None = None
    attribute_groups: list[AttributeGroupType] = strawberry.field(default_factory=list)


@strawberry.type
class ParameterType:
    name: str
    value: JSON | None


@strawberry.type
class StepType:
    id: strawberry.ID
    name: str
    status: str
    create_date: datetime
    modified_date: datetime
    parameters: list[ParameterType] = strawberry.field(default_factory=list)


@strawberry.type
class ProcessTemplateType:
    id: strawberry.ID
    name: str
    version: str
    create_date: datetime
    modified_date: datetime


@strawberry.type
class ResourceAssignmentType:
    resource: ResourceType
    slot_name: str
    direction: str


@strawberry.type
class ProcessRunType:
    id: strawberry.ID
    name: str
    description: str | None
    create_date: datetime
    modified_date: datetime
    process_template: ProcessTemplateType | None = None
    steps: list[StepType] = strawberry.field(default_factory=list)
    resources: list[ResourceAssignmentType] = strawberry.field(default_factory=list)


@strawberry.type
class CampaignType:
    id: strawberry.ID
    name: str
    proposal: str | None
    create_date: datetime
    modified_date: datetime
    process_runs: list[ProcessRunType] = strawberry.field(default_factory=list)
