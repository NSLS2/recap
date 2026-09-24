from __future__ import annotations

from datetime import datetime
from threading import RLock
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from recap.lifecycle import LifecycleStatus
from recap.schemas.attribute import AttributeGroupTemplateSchema
from recap.schemas.common import LoadAware
from recap.schemas.namespace import NamespaceContext, NamespaceRef, NamespaceSchema
from recap.schemas.process import (
    ProcessRunRef,
    ProcessRunSchema,
    ProcessTemplateRef,
    ProcessTemplateSchema,
)
from recap.schemas.resource import (
    ResourceRef,
    ResourceSchema,
    ResourceTemplateRef,
    ResourceTemplateSchema,
)
from recap.schemas.step import (
    StepSchema,
    StepTemplateRef,
    StepTemplateSchema,
)

IdentityKey = tuple[str, UUID]

# Sentinel distinguishing "not yet cached" from a cached ``None`` family.
_MISSING = object()


def _loaded_relations(model: BaseModel) -> dict[str, bool]:
    private = getattr(model, "__pydantic_private__", None) or {}
    value = private.get("_loaded_relations", {})
    return value if isinstance(value, dict) else {}


class IdentityMergeConflict(RuntimeError):
    """Raised when equal revisions contain conflicting scalar state."""


_ENTITY_FAMILIES: tuple[tuple[type[BaseModel], str], ...] = (
    (NamespaceSchema, "namespace"),
    (AttributeGroupTemplateSchema, "attribute_group_template"),
    (NamespaceRef, "namespace"),
    (ResourceTemplateRef, "resource_template"),
    (ResourceTemplateSchema, "resource_template"),
    (ResourceRef, "resource"),
    (ResourceSchema, "resource"),
    (ProcessTemplateRef, "process_template"),
    (ProcessTemplateSchema, "process_template"),
    (ProcessRunRef, "process_run"),
    (ProcessRunSchema, "process_run"),
    (StepTemplateRef, "step_template"),
    (StepTemplateSchema, "step_template"),
    (StepSchema, "step"),
)


class IdentityMap:
    # Concrete model type -> entity family. Populated lazily by ``_family``.
    # Keyed on exact type so the ``_ENTITY_FAMILIES`` isinstance ordering is
    # resolved once per class instead of per value visited.
    _family_cache: dict[type, str | None] = {}

    def __init__(self) -> None:
        self._models: dict[IdentityKey, BaseModel] = {}
        self._lock = RLock()
        self._canonical_merge_pairs: set[tuple[int, int]] = set()
        self._canonicalizing: set[int] = set()
        # id() of canonical instances whose relations are already fully
        # canonicalized. Lets ``_canonicalize_relations`` skip re-walking a
        # shared subtree once per parent that references it. Invalidated
        # whenever a canonical object is mutated by a merge.
        self._canonicalized: set[int] = set()
        # id() -> cached structural signature for canonical instances only.
        # A canonical object's signature is stable between mutations, so
        # equal-revision re-query comparisons reuse it instead of re-hashing
        # the whole graph. Same lifetime/invalidation as ``_canonicalized``.
        self._signature_cache: dict[int, Any] = {}

    def get(self, key: IdentityKey) -> BaseModel | None:
        with self._lock:
            return self._models.get(key)

    def canonical(self, model: BaseModel) -> BaseModel | None:
        family = self._family(model)
        model_id = getattr(model, "id", None)
        if family is None or not isinstance(model_id, UUID):
            return None
        return self.get((family, model_id))

    def intern(self, model: BaseModel, *, authoritative: bool = False) -> BaseModel:
        with self._lock:
            canonical = self._intern(model, authoritative=authoritative)
            self._canonicalize_relations(canonical)
            return canonical

    def clear(self) -> None:
        with self._lock:
            self._models.clear()
            self._canonical_merge_pairs.clear()
            self._canonicalizing.clear()
            self._canonicalized.clear()
            self._signature_cache.clear()

    def _intern(self, model: BaseModel, *, authoritative: bool = False) -> BaseModel:
        family = self._family(model)
        if family is None:
            return model
        model_id = getattr(model, "id", None)
        if not isinstance(model_id, UUID):
            return model

        key = (family, model_id)
        current = self._models.get(key)
        if current is None:
            self._models[key] = model
            self._canonicalize_relations(model)
            return model
        if current is model:
            return current
        self._merge(current, model, authoritative=authoritative)
        return current

    def _is_canonical_instance(self, model: BaseModel) -> bool:
        family = self._family(model)
        if family is None:
            return False
        model_id = getattr(model, "id", None)
        if not isinstance(model_id, UUID):
            return False
        return self._models.get((family, model_id)) is model

    def _invalidate_canonicalized(self, model: BaseModel) -> None:
        """Force ``model`` to be re-walked next time; call before mutating it."""
        self._canonicalized.discard(id(model))
        self._signature_cache.pop(id(model), None)

    def _merge(
        self,
        current: BaseModel,
        incoming: BaseModel,
        *,
        authoritative: bool = False,
    ) -> None:
        self._invalidate_canonicalized(current)
        if (
            isinstance(current, NamespaceRef)
            and not isinstance(current, NamespaceSchema)
            and isinstance(incoming, NamespaceSchema)
        ):
            self._promote_namespace(current, incoming)
            return

        current_revision = getattr(current, "revision", None)
        incoming_revision = getattr(incoming, "revision", None)
        if current_revision is not None and incoming_revision is not None:
            if incoming_revision < current_revision:
                return
            if incoming_revision == current_revision:
                if authoritative:
                    self._canonicalize_relations(incoming)
                    self._invalidate_canonicalized(current)
                    self._merge_loaded_relations(current, incoming)
                    self._copy_fields(current, incoming)
                    self._canonicalize_relations(current)
                    return
                if self._equivalent_with_repeated_array_values(current, incoming):
                    self._invalidate_canonicalized(current)
                    self._copy_fields(current, incoming)
                    self._canonicalize_relations(current)
                    return
                self._raise_on_scalar_conflict(current, incoming)
                self._canonicalize_relations(incoming)
                self._invalidate_canonicalized(current)
                self._merge_loaded_relations(current, incoming)
                return
            self._canonicalize_relations(incoming)
            self._invalidate_canonicalized(current)
            if authoritative:
                self._merge_authoritative_relations(current, incoming)
            else:
                self._replace_loaded_relations(current, incoming)
                self._merge_relation_flags(current, incoming)
            self._copy_fields(current, incoming)
            self._canonicalize_relations(current)
            return

        self._canonicalize_relations(incoming)
        current_date = getattr(current, "modified_date", None)
        incoming_date = getattr(incoming, "modified_date", None)
        if (
            isinstance(current_date, datetime)
            and isinstance(incoming_date, datetime)
            and incoming_date > current_date
        ):
            self._invalidate_canonicalized(current)
            self._merge_loaded_relations(current, incoming)
            self._copy_fields(current, incoming)

    @staticmethod
    def _promote_namespace(current: NamespaceRef, incoming: NamespaceSchema) -> None:
        """Upgrade ref payload in place so existing graph references stay canonical."""
        current.__class__ = NamespaceSchema
        current.__dict__.update(incoming.__dict__)
        current.__pydantic_fields_set__ = set(incoming.__pydantic_fields_set__)

    @staticmethod
    def _copy_fields(current: BaseModel, incoming: BaseModel) -> None:
        for name in incoming.model_fields_set:
            if name in type(current).model_fields:
                if name in getattr(type(current), "_relation_fields", frozenset()):
                    continue
                setattr(current, name, getattr(incoming, name))

    @staticmethod
    def _replace_loaded_relations(current: BaseModel, incoming: BaseModel) -> None:
        incoming_flags = _loaded_relations(incoming)
        relation_fields = getattr(type(current), "_relation_fields", frozenset())
        for name in relation_fields:
            if (
                incoming_flags.get(name) is True or name in incoming.model_fields_set
            ) and name in type(current).model_fields:
                setattr(current, name, incoming.__dict__.get(name))

    @staticmethod
    def _merge_relation_flags(current: BaseModel, incoming: BaseModel) -> None:
        current_flags = _loaded_relations(current)
        incoming_flags = _loaded_relations(incoming)
        if isinstance(current, LoadAware):
            current.set_loaded_relations(
                {
                    name: current_flags.get(name, False) or loaded
                    for name, loaded in {**current_flags, **incoming_flags}.items()
                },
                on_unloaded=(getattr(current, "__pydantic_private__", {}) or {}).get(
                    "_on_unloaded", "warn"
                ),
            )

    def _raise_on_scalar_conflict(self, current: BaseModel, incoming: BaseModel) -> None:  # noqa: C901
        relation_fields = getattr(type(current), "_relation_fields", frozenset())
        for name in sorted(relation_fields):
            if not (
                _loaded_relations(current).get(name) is True
                and _loaded_relations(incoming).get(name) is True
            ):
                continue
            current_value = current.__dict__.get(name)
            incoming_value = incoming.__dict__.get(name)
            if (
                not self._empty_relation_value(current_value)
                and not self._empty_relation_value(incoming_value)
                and self._relation_conflicts(current_value, incoming_value)
            ):
                raise IdentityMergeConflict(
                    f"Conflicting {type(current).__name__} identity {current.id} "
                    f"at equal revision: {name}"
                )

        for name in incoming.model_fields_set & current.model_fields_set:
            if name in {"create_date", "modified_date"}:
                continue
            if name in relation_fields:
                continue
            if (
                _loaded_relations(current).get(name) is False
                or _loaded_relations(incoming).get(name) is False
            ):
                continue
            current_value = current.__dict__.get(name)
            incoming_value = incoming.__dict__.get(name)
            if (
                name == "status"
                and isinstance(current_value, LifecycleStatus)
                and isinstance(incoming_value, LifecycleStatus)
            ):
                continue
            if isinstance(current_value, BaseModel | list | dict) or isinstance(
                incoming_value, BaseModel | list | dict
            ):
                current_loaded = _loaded_relations(current).get(name) is True
                incoming_loaded = _loaded_relations(incoming).get(name) is True
                if (
                    current_loaded
                    and incoming_loaded
                    and self._relation_signature(current_value)
                    != self._relation_signature(incoming_value)
                ):
                    raise IdentityMergeConflict(
                        f"Conflicting {type(current).__name__} identity {current.id} "
                        f"at equal revision: {name}"
                    )
                continue
            if current_value != incoming_value:
                raise IdentityMergeConflict(
                    f"Conflicting {type(current).__name__} identity {current.id} "
                    f"at equal revision: {name}"
                )

    def _relation_signature(
        self,
        value: Any,
    ) -> Any:
        # Per-call memo of ``id(obj) -> signature`` for self-contained
        # identity-bearing models. A shared subtree (e.g. one well template
        # referenced by every well) is hashed once per call instead of once
        # per referencing parent. Safe because objects are immutable for the
        # duration of a signature computation.
        return self._relation_signature_inner(value, set(), set(), {})

    def _relation_signature_inner(  # noqa: C901
        self,
        value: Any,
        _seen: set[tuple[str, UUID]],
        _cycle_hits: set[tuple[str, UUID]],
        _memo: dict[int, Any],
    ) -> Any:
        if isinstance(value, BaseModel):
            family = self._family(value)
            model_id = getattr(value, "id", None)
            if family is not None and isinstance(model_id, UUID):
                identity = (family, model_id)
                if identity in _seen:
                    # Record the back-edge so callers up the stack know their
                    # subtree signature is context-dependent (not cacheable).
                    _cycle_hits.add(identity)
                    return ("cycle", identity)
                obj_key = id(value)
                # Reuse a persisted signature for the canonical instance (state
                # is stable between mutations; its id() stays valid for the
                # map's lifetime), or a within-call memo for any repeated
                # self-contained object.
                is_canonical = self._models.get(identity) is value
                if is_canonical:
                    cached = self._signature_cache.get(obj_key)
                    if cached is not None:
                        return cached
                memoized = _memo.get(obj_key)
                if memoized is not None:
                    return memoized
                _seen.add(identity)
                relation_fields = getattr(type(value), "_relation_fields", frozenset())
                sub_cycles: set[tuple[str, UUID]] = set()
                scalar_state = tuple(
                    (
                        name,
                        self._relation_signature_inner(
                            value.__dict__.get(name), _seen, sub_cycles, _memo
                        ),
                    )
                    for name in sorted(value.model_fields_set - relation_fields)
                    if name not in {"create_date", "modified_date"}
                )
                relation_state = tuple(
                    (
                        name,
                        self._relation_signature_inner(
                            value.__dict__.get(name), _seen, sub_cycles, _memo
                        ),
                    )
                    for name in sorted(relation_fields)
                    if _loaded_relations(value).get(name) is True
                )
                _seen.remove(identity)
                signature = (family, model_id, scalar_state, relation_state)
                # This subtree is context-dependent iff it contained a back-edge
                # to a *proper ancestor* (still on the stack). Cycles that
                # resolved within this subtree (== identity, already removed)
                # do not leak. Only cache/propagate when self-contained.
                external_cycles = sub_cycles - {identity}
                if external_cycles:
                    _cycle_hits |= external_cycles
                else:
                    _memo[obj_key] = signature
                    if is_canonical:
                        self._signature_cache[obj_key] = signature
                return signature
            return tuple(
                (
                    name,
                    self._relation_signature_inner(
                        getattr(value, name), _seen, _cycle_hits, _memo
                    ),
                )
                for name in value.model_fields_set
            )
        if isinstance(value, dict):
            return tuple(
                sorted(
                    (key, self._relation_signature_inner(item, _seen, _cycle_hits, _memo))
                    for key, item in value.items()
                )
            )
        if isinstance(value, list):
            return tuple(
                self._relation_signature_inner(item, _seen, _cycle_hits, _memo)
                for item in value
            )
        return value

    def _relation_conflicts(  # noqa: C901
        self,
        current: Any,
        incoming: Any,
        _seen: set[tuple[int, int]] | None = None,
    ) -> bool:  # noqa: C901
        """Compare only common loaded relation data, not projection shape."""
        if _seen is None:
            _seen = set()
        if isinstance(current, BaseModel) and isinstance(incoming, BaseModel):
            current_id = getattr(current, "id", None)
            incoming_id = getattr(incoming, "id", None)
            if current_id != incoming_id:
                return current != incoming
            current_revision = getattr(current, "revision", None)
            incoming_revision = getattr(incoming, "revision", None)
            if (
                current_revision is not None
                and incoming_revision is not None
                and current_revision != incoming_revision
            ):
                return False
            pair = (id(current), id(incoming))
            if pair in _seen:
                return False
            _seen.add(pair)
            relation_fields = getattr(type(current), "_relation_fields", frozenset())
            for name in (
                current.model_fields_set & incoming.model_fields_set - relation_fields
            ):
                if name in {"create_date", "modified_date", "status"}:
                    continue
                left = current.__dict__.get(name)
                right = incoming.__dict__.get(name)
                if isinstance(left, BaseModel | list | dict) or isinstance(
                    right, BaseModel | list | dict
                ):
                    if self._relation_conflicts(left, right, _seen):
                        return True
                elif left != right:
                    return True
            for name in relation_fields:
                if (
                    _loaded_relations(current).get(name) is True
                    and _loaded_relations(incoming).get(name) is True
                    and self._relation_conflicts(
                        current.__dict__.get(name), incoming.__dict__.get(name), _seen
                    )
                ):
                    return True
            return False
        if isinstance(current, dict) and isinstance(incoming, dict):
            if not (set(current).issubset(incoming) or set(incoming).issubset(current)):
                return True
            return any(
                key in incoming
                and key in current
                and self._relation_conflicts(current[key], incoming[key], _seen)
                for key in current.keys() & incoming.keys()
            )
        if isinstance(current, list) and isinstance(incoming, list):
            if not current or not incoming:
                return False
            current_by_key = {self._value_key(item): item for item in current}
            incoming_by_key = {self._value_key(item): item for item in incoming}
            if not (
                set(current_by_key).issubset(incoming_by_key)
                or set(incoming_by_key).issubset(current_by_key)
            ):
                return True
            return any(
                self._relation_conflicts(
                    current_by_key[key], incoming_by_key[key], _seen
                )
                for key in current_by_key.keys() & incoming_by_key.keys()
            )
        return current != incoming

    def _value_key(self, value: Any) -> Any:
        if isinstance(value, BaseModel):
            model_id = getattr(value, "id", None)
            if isinstance(model_id, UUID):
                return self._family(value), model_id
        return self._relation_signature(value)

    def _equivalent_with_repeated_array_values(
        self, current: Any, incoming: Any
    ) -> bool:
        """Recognize stale hydrated array wrappers without hiding real conflicts."""
        left = self._relation_signature(current)
        right = self._relation_signature(incoming)
        if left == right:
            return True
        return self._dedupe_repeated_sequences(left) == self._dedupe_repeated_sequences(
            right
        )

    def _dedupe_repeated_sequences(self, value: Any) -> Any:
        if isinstance(value, tuple):
            items = tuple(self._dedupe_repeated_sequences(item) for item in value)
            n = len(items)
            if n < 2:
                return items
            # Collapse a tuple that is a whole-number repetition of a shorter
            # block to that block. Only sizes that divide ``n`` can tile it, so
            # iterate divisors (O(d(n))) and verify each candidate period by
            # element comparison with an early exit -- no per-size tuple
            # rebuild. Same result as the original divisor scan.
            for size in range(1, n // 2 + 1):
                if n % size != 0:
                    continue
                if all(items[i] == items[i % size] for i in range(size, n)):
                    return items[:size]
            return items
        return value

    @staticmethod
    def _empty_relation_value(value: Any) -> bool:
        return value is None or value == {} or value == []

    def _relation_is_extension(self, base: Any, candidate: Any) -> bool:
        if isinstance(base, BaseModel) and isinstance(candidate, BaseModel):
            return self._model_is_compatible_extension(base, candidate)
        if isinstance(base, dict) and isinstance(candidate, dict):
            if not set(base).issubset(candidate):
                return False
            return all(
                self._model_is_compatible_extension(base[key], candidate[key])
                for key in base
            )
        if isinstance(base, list) and isinstance(candidate, list):
            candidate_by_id = {
                (self._family(item), getattr(item, "id", None)): item
                for item in candidate
                if isinstance(item, BaseModel)
            }
            for item in base:
                if not isinstance(item, BaseModel):
                    if item not in candidate:
                        return False
                    continue
                match = candidate_by_id.get(
                    (self._family(item), getattr(item, "id", None))
                )
                if match is None or not self._model_is_compatible_extension(item, match):
                    return False
            return True
        return False

    def _model_is_compatible_extension(self, base: Any, candidate: Any) -> bool:  # noqa: C901
        if not isinstance(base, BaseModel) or not isinstance(candidate, BaseModel):
            return base == candidate
        if type(base) is not type(candidate):
            return False
        relation_fields = getattr(type(base), "_relation_fields", frozenset())
        if self._family(base) is None:
            for name in base.model_fields_set & candidate.model_fields_set:
                left = base.__dict__.get(name)
                right = candidate.__dict__.get(name)
                if isinstance(left, BaseModel) and isinstance(right, BaseModel):
                    if not (
                        self._model_is_compatible_extension(left, right)
                        or self._model_is_compatible_extension(right, left)
                    ):
                        return False
                elif isinstance(left, list | dict) or isinstance(right, list | dict):
                    if not (
                        self._relation_is_extension(left, right)
                        or self._relation_is_extension(right, left)
                    ):
                        return False
                elif left != right:
                    return False
            return True
        if getattr(base, "id", None) != getattr(candidate, "id", None):
            return False
        for name in (
            base.model_fields_set & candidate.model_fields_set - relation_fields
        ):
            if name in {"create_date", "modified_date"}:
                continue
            left = base.__dict__.get(name)
            right = candidate.__dict__.get(name)
            if isinstance(left, BaseModel | list | dict) or isinstance(
                right, BaseModel | list | dict
            ):
                if self._relation_signature(left) != self._relation_signature(right):
                    return False
            elif name == "status":
                continue
            elif left != right:
                return False
        for name in relation_fields:
            if (
                _loaded_relations(base).get(name) is True
                and _loaded_relations(candidate).get(name) is True
            ):
                left = base.__dict__.get(name)
                right = candidate.__dict__.get(name)
                if (
                    not self._empty_relation_value(left)
                    and not self._empty_relation_value(right)
                    and not self._relation_is_extension(left, right)
                    and not self._relation_is_extension(right, left)
                ):
                    return False
        return True

    def _canonicalize_relations(self, model: BaseModel) -> None:
        if not isinstance(model, BaseModel):
            return
        model_key = id(model)
        # Already fully canonicalized (and still the canonical stored
        # instance): skip re-walking. This is what keeps a shared subtree
        # from being re-traversed once per referencing parent.
        if model_key in self._canonicalized:
            return
        if model_key in self._canonicalizing:
            return
        self._canonicalizing.add(model_key)
        try:
            loaded = _loaded_relations(model)
            model_dict = model.__dict__
            for name in model.model_fields_set:
                if loaded.get(name) is False:
                    continue
                value = model_dict.get(name)
                # Only models/containers can carry interned identities; scalar
                # fields (ids, names, dates) never do, so skip the dispatch.
                if not isinstance(value, (BaseModel, list, dict)):
                    continue
                canonical = self._canonicalize_value(value)
                if canonical is not value:
                    # Relation state changed: drop any cached signature so a
                    # later equal-revision comparison recomputes it.
                    self._signature_cache.pop(model_key, None)
                    setattr(model, name, canonical)
        finally:
            self._canonicalizing.remove(model_key)
        # Only memoize instances retained as canonical in ``_models``; their
        # id() stays valid for the map's lifetime, so no id-reuse hazard.
        if self._is_canonical_instance(model):
            self._canonicalized.add(model_key)

    def _merge_loaded_relations(self, current: BaseModel, incoming: BaseModel) -> None:
        current_flags = _loaded_relations(current)
        incoming_flags = _loaded_relations(incoming)
        if not current_flags and not incoming_flags:
            return
        merged_flags = dict(current_flags)
        for relation, loaded in incoming_flags.items():
            merged_flags[relation] = merged_flags.get(relation, False) or loaded
            if loaded and relation in type(current).model_fields:
                value = self._canonicalize_value(incoming.__dict__.get(relation))
                existing = current.__dict__.get(relation)
                if (
                    value is not None or existing is None
                ) and not self._merge_container(existing, value):
                    setattr(current, relation, value)
        if isinstance(current, LoadAware):
            current.set_loaded_relations(
                merged_flags,
                on_unloaded=(getattr(current, "__pydantic_private__", {}) or {}).get(
                    "_on_unloaded", "warn"
                ),
            )

    def _merge_authoritative_relations(
        self, current: BaseModel, incoming: BaseModel
    ) -> None:
        """Apply command responses even when transport omits load metadata."""
        self._merge_loaded_relations(current, incoming)
        for name in getattr(type(current), "_relation_fields", frozenset()):
            if name in incoming.model_fields_set and name in type(current).model_fields:
                setattr(
                    current, name, self._canonicalize_value(incoming.__dict__.get(name))
                )

    def _merge_container(  # noqa: C901
        self,
        current: Any,
        incoming: Any,
        *,
        _seen: set[tuple[int, int]] | None = None,
    ) -> bool:
        if _seen is None:
            _seen = set()
        if isinstance(current, BaseModel | list | dict) and isinstance(
            incoming, BaseModel | list | dict
        ):
            pair = (id(current), id(incoming))
            if pair in _seen:
                return True
            _seen.add(pair)
        if isinstance(current, dict) and isinstance(incoming, dict):
            for key, value in incoming.items():
                existing = current.get(key)
                if not self._merge_container(existing, value, _seen=_seen):
                    current[key] = value
            return True
        if isinstance(current, list) and isinstance(incoming, list):
            existing = {self._value_key(item): item for item in current}
            for item in incoming:
                key = self._value_key(item)
                if key not in existing:
                    current.append(item)
                    existing[key] = item
                else:
                    self._merge_container(existing[key], item, _seen=_seen)
            return True
        if isinstance(current, BaseModel) and isinstance(incoming, BaseModel):
            relation_fields = getattr(type(current), "_relation_fields", frozenset())
            for name in incoming.model_fields_set:
                if name not in type(current).model_fields:
                    continue
                value = incoming.__dict__.get(name)
                if name in relation_fields:
                    existing = current.__dict__.get(name)
                    if not self._merge_container(existing, value, _seen=_seen):
                        setattr(current, name, self._canonicalize_value(value))
                else:
                    setattr(current, name, self._canonicalize_value(value))
            return True
        return False

    def _canonicalize_value(self, value: Any) -> Any:
        if isinstance(value, BaseModel):
            family = self._family(value)
            model_id = getattr(value, "id", None)
            if family is not None and isinstance(model_id, UUID):
                existing = self._models.get((family, model_id))
                if existing is not None:
                    if existing is value:
                        return existing
                    pair = (id(existing), id(value))
                    if pair not in self._canonical_merge_pairs:
                        self._canonical_merge_pairs.add(pair)
                        self._invalidate_canonicalized(existing)
                        try:
                            self._merge_loaded_relations(existing, value)
                        finally:
                            self._canonical_merge_pairs.remove(pair)
                    return existing
            self._canonicalize_relations(value)
            return self._intern(value)
        if isinstance(value, list):
            return [self._canonicalize_value(item) for item in value]
        if isinstance(value, dict):
            return {key: self._canonicalize_value(item) for key, item in value.items()}
        return value

    @classmethod
    def _family(cls, model: BaseModel) -> str | None:
        model_type = type(model)
        cache = cls._family_cache
        family = cache.get(model_type, _MISSING)
        if family is _MISSING:
            family = cls._resolve_family(model)
            cache[model_type] = family
        return family

    @staticmethod
    def _resolve_family(model: BaseModel) -> str | None:
        if isinstance(model, NamespaceContext):
            return None
        for model_type, family in _ENTITY_FAMILIES:
            if isinstance(model, model_type):
                return family
        return None
