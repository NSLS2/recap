from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from recap.lifecycle import LifecycleStatus


class RestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateNamespaceRequest(RestModel):
    metadata: dict[str, Any] = Field(default_factory=dict)


class UpdateNamespaceRequest(RestModel):
    metadata: dict[str, Any] | None = None
    status: LifecycleStatus | None = None

    @model_validator(mode="after")
    def require_change(self):
        if self.metadata is None and self.status is None:
            raise ValueError("Namespace update is empty")
        return self
