"""Strong canonical storage; graph reconciliation belongs to BatchMaterializer."""

from collections.abc import Iterator
from contextlib import contextmanager
from threading import RLock
from uuid import UUID

from pydantic import BaseModel

from recap.adapter.entity_records import POLICIES_BY_KIND

IdentityKey = tuple[str, UUID]


class IdentityMap:
    def __init__(self) -> None:
        self._models: dict[IdentityKey, BaseModel] = {}
        self._lock = RLock()

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Serialize an entire materialization with lookup, registration, and clear."""
        with self._lock:
            yield

    def get(self, key: IdentityKey) -> BaseModel | None:
        with self._lock:
            return self._models.get(key)

    def register(self, key: IdentityKey, model: BaseModel) -> BaseModel:
        """Register one canonical instance without inspecting or merging its graph."""
        kind, entity_id = key
        policy = POLICIES_BY_KIND.get(kind)
        if (
            policy is None
            or policy.ownership != "canonical"
            or not isinstance(entity_id, UUID)
            or not isinstance(model, policy.schema)
            or model.id != entity_id
        ):
            raise ValueError(
                "Canonical registration requires matching entity kind and id"
            )
        with self._lock:
            current = self._models.get(key)
            if current is not None and current is not model:
                raise ValueError(f"Canonical instance already registered: {key}")
            self._models[key] = model
            return model

    def clear(self) -> None:
        with self._lock:
            self._models.clear()
