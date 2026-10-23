"""Build normalized fixture batches for recording backend test doubles."""

from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel

from recap.adapter.entity_records import (
    POLICIES_BY_KIND,
    EntityBatch,
    EntityRef,
    NamedAdjacency,
    SequenceAdjacency,
    SingleAdjacency,
)
from recap.client.materialization import RELATIONS

_STAMP = datetime(2026, 1, 1)


def model_batch(schema, models):
    """Normalize model fixtures; production adapters never call this helper."""
    kind = next(
        kind for kind, policy in POLICIES_BY_KIND.items() if policy.schema is schema
    )
    records, edges, roots = {}, [], []

    def collect(source, kind):
        policy = POLICIES_BY_KIND[kind]
        values = source.__dict__ if hasattr(source, "__dict__") else dict(source)
        entity_id = values.get("id") or getattr(source, "id", None) or uuid4()
        ref = EntityRef(kind=kind, id=entity_id)
        if ref in records:
            return ref
        defaults = {
            "create_date": _STAMP,
            "modified_date": _STAMP,
            "namespace_id": uuid4(),
            "name": "fixture",
            "slug": "fixture",
            "version": "1.0",
            "status": "ACTIVE",
            "value_type": "int",
            "default_value": None,
            "unit": None,
        }
        fields = {
            name: values.get(name, defaults.get(name))
            for name, field in policy.schema.model_fields.items()
            if name not in {"id", "revision"}
            and name not in policy.relations
            and (name in values or field.is_required())
        }
        kwargs = (
            {"revision": values.get("revision", 1)}
            if "revision" in policy.schema.model_fields
            else {}
        )
        records[ref] = policy.record(id=entity_id, fields=fields, **kwargs)
        for relation, (shape, target_kind) in RELATIONS.get(kind, {}).items():
            if relation not in values:
                continue
            value = values[relation]
            loaded = (
                (getattr(source, "__pydantic_private__", None) or {})
                .get("_loaded_relations", {})
                .get(relation, True)
            )
            if shape == "single":
                edges.append(
                    SingleAdjacency(
                        source=ref,
                        relation=relation,
                        target=None if value is None else collect(value, target_kind),
                        loaded=loaded,
                    )
                )
            elif shape == "named":
                items = value.items() if hasattr(value, "items") else []
                edges.append(
                    NamedAdjacency(
                        source=ref,
                        relation=relation,
                        targets={
                            name: collect(target, target_kind) for name, target in items
                        },
                        loaded=loaded,
                    )
                )
            else:
                edges.append(
                    SequenceAdjacency(
                        source=ref,
                        relation=relation,
                        targets=[collect(target, target_kind) for target in value],
                        loaded=loaded,
                    )
                )
        return ref

    for model in models:
        roots.append(collect(model, kind))
    return EntityBatch(roots=roots, entities=list(records.values()), adjacency=edges)


class BatchReaderFake:
    def query_batch(self, schema, spec, *, namespace_path):
        return model_batch(
            schema, self.query(schema, spec, namespace_path=namespace_path)
        )


class BatchWriterFake:
    def execute_batch(self, command, context, *, etag_override=None):
        result = (
            self.execute(command, context)
            if etag_override is None
            else self.execute(command, context, etag_override=etag_override)
        )
        if not isinstance(result, BaseModel):
            return result
        return model_batch(type(result), [result])
