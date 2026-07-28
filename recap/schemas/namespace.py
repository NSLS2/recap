from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from recap.lifecycle import LifecycleStatus
from recap.schemas.common import CommonFields


class NamespaceRef(BaseModel):
    id: UUID
    path: str

    model_config = ConfigDict(from_attributes=True)


class NamespaceContext(NamespaceRef):
    pass


class NamespaceSchema(CommonFields):
    path: str
    parent_id: UUID | None = None
    status: LifecycleStatus
    revision: int
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias="metadata_json",
        serialization_alias="metadata",
    )
