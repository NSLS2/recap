from __future__ import annotations

import hashlib
import json
from uuid import UUID

from pydantic import BaseModel


def command_fingerprint(
    *,
    method: str,
    route_template: str,
    namespace_path: str | None,
    source_id: UUID | None,
    body: BaseModel,
) -> str:
    payload = {
        "method": method,
        "route": route_template,
        "namespace": namespace_path,
        "source_id": str(source_id) if source_id else None,
        "body": body.model_dump(mode="json", exclude_none=False),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
