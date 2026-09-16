from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from recap.lifecycle import LifecycleStatus
from recap.schemas.common import CommonFields


class NamespaceRef(BaseModel):
    """Lightweight namespace identity used in relationships and query results."""

    id: UUID
    path: str

    model_config = ConfigDict(from_attributes=True)


class NamespaceContext(NamespaceRef):
    """Active namespace scope attached to client queries and writes."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    metadata: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias="metadata_json",
        serialization_alias="metadata",
    )
    status: LifecycleStatus | None = None
    revision: int | None = None
    etag: str | None = None

    @model_validator(mode="after")
    def populate_etag(self):
        if self.etag is None and self.revision is not None:
            self.etag = f'"{self.revision}"'
        return self

class NamespaceSchema(CommonFields):
    """Persisted namespace with lifecycle, hierarchy, and metadata fields."""

    path: str
    parent_id: UUID | None = None
    status: LifecycleStatus
    revision: int
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias="metadata_json",
        serialization_alias="metadata",
    )
