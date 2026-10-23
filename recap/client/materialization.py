"""Materialize validated flat records into canonical public client models."""

from functools import lru_cache

from pydantic import BaseModel, TypeAdapter

from recap.adapter.entity_records import (
    POLICIES_BY_KIND,
    EntityBatch,
    EntityRef,
    NamedAdjacency,
    RevisionedRecord,
    SingleAdjacency,
)
from recap.adapter.hydration_fields import attribute_values_model
from recap.exceptions import RecapProtocolError
from recap.schemas.attribute import AttributeValueSchema
from recap.schemas.resource import ResourceAssignmentSchema
from recap.utils.dsl import build_property_groups_model, build_step_parameters_model
from recap.utils.general import from_json_value, validate_enum_value

# Public relationship shapes and target kinds are closed, unlike record fields.
RELATIONS = {
    "resource": {
        "template": ("single", "resource_template"),
        "parent": ("single", "resource"),
        "children": ("named", "resource"),
        "properties": ("named", "property"),
    },
    "resource_template": {
        "parent": ("single", "resource_template"),
        "children": ("named", "resource_template"),
        "types": ("sequence", "resource_type"),
        "attribute_group_templates": ("sequence", "attribute_group_template"),
    },
    "process_run": {
        "template": ("single", "process_template"),
        "steps": ("named", "step"),
        "assigned_resources": ("named", "resource_assignment"),
    },
    "process_template": {
        "step_templates": ("named", "step_template"),
        "resource_slots": ("sequence", "resource_slot"),
    },
    "step": {
        "template": ("single", "step_template"),
        "children": ("named", "step"),
        "parameters": ("named", "parameter"),
        "resources": ("named", "resource"),
        "resource_overrides": ("named", "resource"),
    },
    "step_template": {
        "attribute_group_templates": ("sequence", "attribute_group_template"),
        "resource_slots": ("named", "resource_slot"),
    },
    "attribute_group_template": {
        "attribute_templates": ("sequence", "attribute_template")
    },
    "property": {
        "template": ("single", "attribute_group_template"),
        "values": ("named", "attribute_value"),
    },
    "parameter": {
        "template": ("single", "attribute_group_template"),
        "values": ("named", "attribute_value"),
    },
    "resource_slot": {"resource_type": ("single", "resource_type")},
    "attribute_value": {"template": ("single", "attribute_template")},
    "resource_assignment": {
        "slot": ("single", "resource_slot"),
        "resource": ("single", "resource"),
    },
}


@lru_cache(maxsize=512)
def _field_adapter(schema, name):
    return TypeAdapter(schema.model_fields[name].rebuild_annotation())


def _items(value):
    if isinstance(value, BaseModel):
        return {
            field.alias or name: value.__dict__.get(name)
            for name, field in type(value).model_fields.items()
        }
    return value if isinstance(value, dict) else {}


class BatchMaterializer:
    """Apply a batch under the identity map's lock without recursive signatures.

    Backend records are authoritative for supplied fields. Omitted/unloaded
    edges preserve cached state; older entity revisions do not overwrite it.
    Owned models are reused only through their current owning containers.
    """

    def __init__(self, identity_map):
        self.identity_map = identity_map

    def materialize(self, schema, batch: EntityBatch):
        with self.identity_map._lock:
            return self._materialize(schema, batch)

    def _validate(self, schema, batch):
        records = {}
        for record in batch.entities:
            ref = EntityRef(kind=record.kind, id=record.id)
            if ref in records:
                raise RecapProtocolError(f"Duplicate entity record: {ref}")
            records[ref] = record
        for root in batch.roots:
            if root not in records or POLICIES_BY_KIND[root.kind].schema is not schema:
                raise RecapProtocolError("Batch root does not match requested schema")
        edges = {}
        for edge in batch.adjacency:
            key = (edge.source, edge.relation)
            if key in edges:
                raise RecapProtocolError(f"Duplicate relationship: {edge.relation}")
            policy = RELATIONS.get(edge.source.kind, {}).get(edge.relation)
            if edge.source not in records or policy is None or policy[0] != edge.shape:
                raise RecapProtocolError(f"Invalid relationship: {edge.relation}")
            for target in edge.references():
                if target not in records or target.kind != policy[1]:
                    raise RecapProtocolError(
                        f"Invalid relationship target: {edge.relation}"
                    )
            edges[key] = edge
        self._validate_required_relations(records, edges)
        return records, edges

    def _validate_required_relations(self, records, edges):
        for ref in records:
            policy = POLICIES_BY_KIND[ref.kind]
            schema = policy.schema
            if schema is None:
                continue
            current = self.identity_map.get((ref.kind, ref.id))
            for name in policy.relations:
                field = schema.model_fields.get(name)
                # Persisted aggregates need template support even though draft
                # schemas allow an absent template during builder construction.
                required = field is not None and (
                    field.is_required() or name == "template"
                )
                if not required:
                    continue
                edge = edges.get((ref, name))
                cached = current is not None and name in current.__dict__
                if (edge is None and not cached) or (
                    isinstance(edge, SingleAdjacency) and edge.target is None
                ):
                    raise RecapProtocolError(
                        f"Missing required {ref.kind} relationship: {name}"
                    )

    def _fields(self, record):
        policy = POLICIES_BY_KIND[record.kind]
        schema = policy.schema
        if schema is None:
            return dict(record.fields)
        fields = {}
        for name, value in record.fields.items():
            if (
                name in {"id", "revision"}
                or name in policy.relations
                or name not in schema.model_fields
            ):
                raise RecapProtocolError(f"Invalid {record.kind} field: {name}")
            fields[name] = _field_adapter(schema, name).validate_python(value)
        fields["id"] = record.id
        if record.kind == "attribute_template" and "default_value" in fields:
            fields["default_value"] = from_json_value(
                fields["value_type"], fields["default_value"]
            )
        if isinstance(record, RevisionedRecord):
            fields["revision"] = record.revision
        current = self.identity_map.get((record.kind, record.id))
        available = fields.keys() | (
            current.__dict__.keys() if current is not None else set()
        )
        missing = {
            name
            for name, field in schema.model_fields.items()
            if field.is_required()
            and name not in policy.relations
            and name not in available
        }
        if missing:
            raise RecapProtocolError(
                f"Missing required {record.kind} fields: {sorted(missing)}"
            )
        return fields

    def _materialize(self, schema, batch):
        records, edges = self._validate(schema, batch)
        # Validate data before touching any existing canonical state.
        fields = {ref: self._fields(record) for ref, record in records.items()}
        validated_values = self._validate_values(records, edges, fields)
        models, accepted = {}, {}
        for ref, record in records.items():
            policy = POLICIES_BY_KIND[ref.kind]
            current = (
                self.identity_map.get((ref.kind, ref.id))
                if policy.ownership == "canonical"
                else None
            )
            current_revision = getattr(current, "revision", None)
            accepted[ref] = not (
                isinstance(record, RevisionedRecord)
                and current_revision is not None
                and record.revision < current_revision
            )
            model_schema = policy.schema
            if model_schema is None:
                model_schema = (
                    ResourceAssignmentSchema
                    if ref.kind == "resource_assignment"
                    else AttributeValueSchema
                )
            models[ref] = current or model_schema.model_construct(**fields[ref])
        self._reuse_owned(models, accepted, edges)
        self._reject_owned_steps(accepted, edges)
        # Attach all ordinary references before constructing dependent values.
        for (ref, relation), edge in edges.items():
            if (
                not accepted[ref]
                or relation == "values"
                or ref.kind == "attribute_value"
            ):
                continue
            model = models[ref]
            if not edge.loaded and model.__dict__.get(relation) is not None:
                continue
            value = self._edge_value(edge, models)
            if relation == "resource_overrides":
                value = {name: target.id for name, target in edge.targets.items()}
            self._assign_relation(model, relation, value, edge.complete)
        for ref in records:
            if accepted[ref] and ref.kind not in {
                "attribute_value",
                "resource_assignment",
            }:
                self._apply_fields(models[ref], fields[ref])
        self._build_values(models, accepted, edges, validated_values)
        self._build_groups(models, accepted, edges)
        self._store(models, accepted, edges, batch.on_unloaded)
        return [models[root] for root in batch.roots]

    def _store(self, models, accepted, edges, on_unloaded):
        by_source = {}
        for (source, relation), edge in edges.items():
            by_source.setdefault(source, {})[relation] = edge.loaded
        for ref, model in models.items():
            policy = POLICIES_BY_KIND[ref.kind]
            if policy.ownership != "canonical":
                continue
            if accepted[ref]:
                self.identity_map._invalidate_canonicalized(model)
                if hasattr(model, "set_loaded_relations"):
                    old = (model.__pydantic_private__ or {}).get(
                        "_loaded_relations", {}
                    )
                    flags = {
                        name: old.get(name, False)
                        for name in getattr(type(model), "_relation_fields", ())
                    }
                    for relation, loaded in by_source.get(ref, {}).items():
                        if relation in flags:
                            flags[relation] = flags[relation] or loaded
                    model.set_loaded_relations(flags, on_unloaded=on_unloaded)
            self.identity_map._models[ref.kind, ref.id] = model

    @staticmethod
    def _apply_fields(model, fields):
        for name, value in fields.items():
            if name in type(model).model_fields and model.__dict__.get(name) != value:
                # Data has already passed destination field validation. Do not
                # trigger draft observers for backend refreshes.
                model.__dict__[name] = value
        model.__pydantic_fields_set__.update(
            name for name in fields if name in type(model).model_fields
        )

    @staticmethod
    def _edge_value(edge, models):
        if isinstance(edge, SingleAdjacency):
            return None if edge.target is None else models[edge.target]
        if isinstance(edge, NamedAdjacency):
            return {name: models[target] for name, target in edge.targets.items()}
        return [models[target] for target in edge.targets]

    @staticmethod
    def _assign_relation(model, name, value, complete):
        existing = model.__dict__.get(name)
        if isinstance(existing, BaseModel) and isinstance(value, dict):
            return  # Generated group/value containers are handled explicitly below.
        if isinstance(existing, dict) and isinstance(value, dict):
            if complete:
                for key in list(existing):
                    if key not in value:
                        del existing[key]
            existing.update(value)
        elif isinstance(existing, list) and isinstance(value, list):
            if complete:
                existing[:] = value
            else:
                for item in value:
                    if not any(item is current for current in existing):
                        existing.append(item)
        else:
            model.__dict__[name] = value
        model.__pydantic_fields_set__.add(name)

    @staticmethod
    def _reuse_owned(models, accepted, edges):
        # Parent-owned groups and assignment views retain references across reads.
        for (source, relation), edge in edges.items():
            if not isinstance(edge, NamedAdjacency) or relation not in {
                "properties",
                "parameters",
                "assigned_resources",
            }:
                continue
            existing = _items(models[source].__dict__.get(relation))
            for name, ref in edge.targets.items():
                current = existing.get(name)
                if current is None:
                    current = next(
                        (
                            group
                            for group in existing.values()
                            if getattr(group, "id", None) == ref.id
                        ),
                        None,
                    )
                if current is not None and (getattr(current, "id", ref.id) == ref.id):
                    models[ref] = current
                if not accepted[source]:
                    accepted[ref] = False

    @staticmethod
    def _reject_owned_steps(accepted, edges):
        # Steps have no independent revision. A rejected run response must not
        # mutate its step state or owned parameters through nested records.
        pending = [ref for ref, accept in accepted.items() if not accept]
        children = {}
        for (source, relation), edge in edges.items():
            if (source.kind == "process_run" and relation == "steps") or (
                source.kind == "step" and relation in {"children", "parameters"}
            ):
                children.setdefault(source, []).extend(edge.references())
        while pending:
            source = pending.pop()
            for target in children.get(source, ()):
                if accepted[target]:
                    accepted[target] = False
                    pending.append(target)

    def _validate_values(self, records, edges, fields):
        templates = {}
        for ref in records:
            if ref.kind == "attribute_template":
                templates[ref] = POLICIES_BY_KIND[ref.kind].schema.model_construct(
                    **fields[ref]
                )
        groups = {}
        for ref in records:
            if ref.kind == "attribute_group_template":
                edge = edges.get((ref, "attribute_templates"))
                groups[ref] = POLICIES_BY_KIND[ref.kind].schema.model_construct(
                    **fields[ref],
                    attribute_templates=[]
                    if edge is None
                    else [templates[target] for target in edge.targets],
                )
        validated_values = {}
        cached_groups = self._cached_groups(edges)
        for (ref, relation), edge in edges.items():
            if relation != "values":
                continue
            template_edge = edges.get((ref, "template"))
            if template_edge is None or template_edge.target not in groups:
                raise RecapProtocolError("Attribute group is missing its template")
            template = groups[template_edge.target]
            unknown = edge.targets.keys() - {
                attribute.name for attribute in template.attribute_templates
            }
            if unknown:
                raise RecapProtocolError(f"Unknown attribute names: {sorted(unknown)}")
            value_type = attribute_values_model(template)
            payload = self._value_payload(
                template, edge, records, cached_groups.get(ref)
            )
            validated_values[ref] = value_type.model_validate(payload)
        return validated_values

    def _cached_groups(self, edges):
        groups = {}
        for (source, relation), edge in edges.items():
            if relation not in {"properties", "parameters"}:
                continue
            owner = self.identity_map.get((source.kind, source.id))
            if owner is None:
                continue
            current = _items(owner.__dict__.get(relation))
            for name, target in edge.targets.items():
                group = current.get(name)
                if group is None:
                    group = next(
                        (item for item in current.values() if item.id == target.id),
                        None,
                    )
                if group is not None and group.id == target.id:
                    groups[target] = group
        return groups

    @staticmethod
    def _value_payload(template, edge, records, current):
        existing = _items(current.__dict__.get("values")) if current is not None else {}
        payload = {}
        for attribute in template.attribute_templates:
            target = edge.targets.get(attribute.name)
            old = existing.get(attribute.name)
            defaults = {"value": None, "unit": attribute.unit, "metadata_json": {}}
            # Completeness describes membership, not supplied scalar fields.
            # Present values retain omitted fields; absent values retain state
            # only for partial adjacency.
            if old is not None and (target is not None or not edge.complete):
                defaults.update(old.__dict__)
            data = defaults | (records[target].fields if target is not None else {})
            if attribute.value_type == "enum":
                validate_enum_value(
                    data["value"], attribute.metadata, name=attribute.name
                )
            payload[attribute.slug] = {
                name: data[name] for name in ("value", "unit", "metadata_json")
            }
        return payload

    def _build_values(self, models, accepted, edges, validated_values):
        for ref, validated in validated_values.items():
            if not accepted[ref]:
                continue
            group = models[ref]
            value_type = type(validated)
            current = group.__dict__.get("values")
            if type(current) is value_type:
                for name in value_type.model_fields:
                    existing = current.__dict__[name]
                    self._apply_fields(existing, validated.__dict__[name].__dict__)
            else:
                group.__dict__["values"] = validated
            group.__pydantic_fields_set__.add("values")

    @staticmethod
    def _build_groups(models, accepted, edges):
        for (ref, relation), edge in edges.items():
            if relation not in {"properties", "parameters"} or not accepted[ref]:
                continue
            model = models[ref]
            groups = {name: models[target] for name, target in edge.targets.items()}
            if not edge.complete:
                groups = _items(model.__dict__.get(relation)) | groups
            template = model.__dict__["template"]
            fields = tuple(
                (group.template.slug or group.template.name, group.template.name)
                for group in groups.values()
            )
            if not fields:
                model.__dict__[relation] = {}
                continue
            factory = (
                build_property_groups_model
                if relation == "properties"
                else build_step_parameters_model
            )
            name = (
                (template.slug or template.name)
                if relation == "properties"
                else template.name
            )
            group_type = factory(name, fields)
            values = {
                group.template.slug or group.template.name: group
                for group in groups.values()
            }
            current = model.__dict__.get(relation)
            if type(current) is group_type:
                current.__dict__.update(values)
            else:
                model.__dict__[relation] = group_type.model_construct(**values)
