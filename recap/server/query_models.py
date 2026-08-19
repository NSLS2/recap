from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class QueryRPCRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    entity: str
    projection: Literal["full", "ref"]
    namespace_path: str
    spec: dict[str, Any]
