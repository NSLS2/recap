"""Direct ORM-to-record collection without constructing a public model graph."""

from collections import deque

from recap.adapter.entity_records import (
    POLICIES_BY_ORM,
    EntityBatch,
    EntityRef,
    NamedAdjacency,
    SequenceAdjacency,
    SingleAdjacency,
)
from recap.adapter.hydration_fields import extract_model_fields, extract_simple_fields
from recap.adapter.hydration_relations import (
    process_run_relations,
    process_template_relations,
    resource_relations,
    resource_template_relations,
    step_template_relations,
)
from recap.schemas.attribute import AttributeTemplateSchema
from recap.schemas.namespace import NamespaceSchema
from recap.utils.general import from_json_value

__all__ = [
    "EntityBatch",
    "EntityBatchCollector",
    "EntityRef",
    "NamedAdjacency",
    "SequenceAdjacency",
    "SingleAdjacency",
]


class EntityBatchCollector:
    """Merge requested coverage per identity using an operation-local work queue.

    Relationships not requested are never visited. A later occurrence can add
    coverage to an already registered row without duplicating its record.
    """

    def __init__(self, *, children_map=None, on_unloaded="warn"):
        self.children_map = children_map
        self.on_unloaded = on_unloaded
        self._roots = []
        self._records = {}
        self._coverage = {}
        self._edges = {}
        self._queue = deque()

    def _enqueue(self, row, requested=frozenset()):
        policy = POLICIES_BY_ORM[type(row)]
        unsupported = set(requested) - policy.relations
        if unsupported:
            raise ValueError(
                f"Unsupported {policy.kind} relationships: {sorted(unsupported)}"
            )
        ref = EntityRef(kind=policy.kind, id=row.id)
        self._queue.append((row, ref, set(requested)))
        return ref

    def add_root(self, row, requested):
        self._roots.append(self._enqueue(row, requested))

    def collect(self) -> EntityBatch:
        while self._queue:
            row, ref, requested = self._queue.popleft()
            if ref not in self._records:
                self._records[ref] = self._record(row, ref)
                self._coverage[ref] = set()
            new = requested - self._coverage[ref]
            if not new:
                continue
            self._coverage[ref].update(new)
            getattr(self, f"_expand_{ref.kind}")(row, ref, new)
        return EntityBatch(
            roots=self._roots,
            entities=list(self._records.values()),
            adjacency=list(self._edges.values()),
            on_unloaded=self.on_unloaded,
        )

    def _record(self, row, ref):
        policy = POLICIES_BY_ORM[type(row)]
        if ref.kind == "namespace":
            fields = extract_model_fields(NamespaceSchema, row)
            fields["metadata"] = row.metadata_json
        elif ref.kind == "attribute_template":
            fields = extract_model_fields(AttributeTemplateSchema, row)
        elif ref.kind == "attribute_value":
            fields = {
                "create_date": row.create_date,
                "modified_date": row.modified_date,
                "value": from_json_value(row.template.value_type, row.value_json),
                "unit": row.unit,
                "metadata_json": row.metadata_json,
                "property_id": row.property_id,
                "parameter_id": row.parameter_id,
            }
        elif ref.kind == "resource_assignment":
            fields = {
                "process_run_id": row.process_run_id,
                "step_id": row.step_id,
                "resource_slot_id": row.resource_slot_id,
                "resource_id": row.resource_id,
            }
        else:
            fields = extract_simple_fields(policy.schema, row)
        # Existing public models retain minimal template references even when
        # those relations are not requested. These are supporting records,
        # explicitly marked unloaded rather than advertised as included data.
        if ref.kind in {"resource", "process_run", "step"}:
            self._single(ref, "template", row.template, loaded=False)
        if ref.kind == "resource_slot":
            self._single(ref, "resource_type", row.resource_type)
        fields.pop("id", None)
        kwargs = {}
        if "revision" in fields:
            kwargs["revision"] = fields.pop("revision")
        return policy.record(id=ref.id, fields=fields, **kwargs)

    def _single(self, source, relation, row, requested=frozenset(), *, loaded=True):
        target = None if row is None else self._enqueue(row, requested)
        self._edges[source, relation] = SingleAdjacency(
            source=source, relation=relation, target=target, loaded=loaded
        )

    def _named(self, source, relation, rows, requested=frozenset()):
        targets = {name: self._enqueue(row, requested) for name, row in rows.items()}
        self._edges[source, relation] = NamedAdjacency(
            source=source, relation=relation, targets=targets
        )

    def _sequence(self, source, relation, rows, requested=frozenset()):
        targets = [self._enqueue(row, requested) for row in rows]
        self._edges[source, relation] = SequenceAdjacency(
            source=source, relation=relation, targets=targets
        )

    def _expand_resource(self, row, ref, requested):
        relations = resource_relations(row, requested, self.children_map)
        if "template" in requested:
            self._single(
                ref,
                "template",
                relations["template"],
                {"types", "parent", "children", "attribute_group_templates"},
            )
        if "parent" in requested:
            self._single(ref, "parent", relations["parent"], {"template"})
        if "children" in requested:
            # Children inherit accumulated requested resource coverage, not just
            # the latest occurrence's projection upgrade.
            self._named(ref, "children", relations["children"], self._coverage[ref])
        if "properties" in requested:
            self._named(
                ref, "properties", relations["properties"], {"template", "values"}
            )
        if "children" in self._coverage[ref] and "children" not in requested:
            children = resource_relations(row, {"children"}, self.children_map)[
                "children"
            ]
            for child in children.values():
                self._enqueue(child, requested)

    def _expand_resource_template(self, row, ref, requested):
        relations = resource_template_relations(row, requested)
        if "types" in requested:
            self._sequence(ref, "types", relations["types"])
        if "parent" in requested:
            self._single(ref, "parent", relations["parent"], {"parent", "types"})
        if "children" in requested:
            self._named(
                ref,
                "children",
                relations["children"],
                {"parent", "types", "children", "attribute_group_templates"},
            )
        if "attribute_group_templates" in requested:
            self._sequence(
                ref,
                "attribute_group_templates",
                relations["attribute_group_templates"],
                {"attribute_templates"},
            )

    def _expand_process_template(self, row, ref, requested):
        relations = process_template_relations(row, requested)
        if "step_templates" in requested:
            self._named(
                ref,
                "step_templates",
                relations["step_templates"],
                {"attribute_group_templates", "resource_slots"},
            )
        if "resource_slots" in requested:
            self._sequence(
                ref, "resource_slots", relations["resource_slots"], {"resource_type"}
            )

    def _expand_step_template(self, row, ref, requested):
        relations = step_template_relations(row, requested)
        if "attribute_group_templates" in requested:
            self._sequence(
                ref,
                "attribute_group_templates",
                relations["attribute_group_templates"],
                {"attribute_templates"},
            )
        if "resource_slots" in requested:
            self._named(
                ref, "resource_slots", relations["resource_slots"], {"resource_type"}
            )

    def _expand_attribute_group_template(self, row, ref, requested):
        self._sequence(ref, "attribute_templates", row.attribute_templates)

    def _expand_resource_slot(self, row, ref, requested):
        self._single(ref, "resource_type", row.resource_type)

    def _expand_property(self, row, ref, requested):
        if "template" in requested:
            self._single(ref, "template", row.template, {"attribute_templates"})
        if "values" in requested:
            self._named(ref, "values", row._values, {"template"})

    _expand_parameter = _expand_property

    def _expand_attribute_value(self, row, ref, requested):
        self._single(ref, "template", row.template)

    def _expand_resource_assignment(self, row, ref, requested):
        if "slot" in requested:
            self._single(ref, "slot", row.resource_slot, {"resource_type"})
        if "resource" in requested:
            self._single(
                ref,
                "resource",
                row.resource,
                {"template", "parent", "children", "properties"},
            )

    def _expand_process_run(self, row, ref, requested):
        coverage = self._coverage[ref]
        if "template" in requested:
            self._single(
                ref, "template", row.template, {"step_templates", "resource_slots"}
            )
        steps_requested = bool(coverage & {"steps", "steps.parameters", "resources"})
        relations = process_run_relations(row, {"steps"} if steps_requested else set())
        if steps_requested:
            step_coverage = {"children"}
            if "steps.parameters" in coverage:
                step_coverage.add("parameters")
            if "resources" in coverage:
                step_coverage.update({"resources", "resource_overrides"})
            if "template" in coverage:
                step_coverage.add("template")
            steps = relations["steps"]
            self._named(
                ref,
                "steps",
                {step.name: step for step in steps.values() if step.parent_id is None},
                step_coverage,
            )
            for step in steps.values():
                self._enqueue(step, step_coverage)
        if coverage & {"resources", "assigned_resources"}:
            self._named(
                ref,
                "assigned_resources",
                {
                    assignment.resource_slot.name: assignment
                    for assignment in row.assignments.values()
                },
                {"slot", "resource"},
            )

    def _expand_step(self, row, ref, requested):
        if "template" in requested:
            self._single(
                ref,
                "template",
                row.template,
                {"attribute_group_templates", "resource_slots"},
            )
        if "parameters" in requested:
            self._named(ref, "parameters", row.parameters, {"template", "values"})
        if "children" in requested:
            self._named(
                ref,
                "children",
                {child.name: child for child in row.children},
                self._coverage[ref],
            )
        if "resources" in requested:
            self._named(
                ref,
                "resources",
                row.resources,
                {"template", "parent", "children", "properties"},
            )
        if "resource_overrides" in requested:
            overrides = {}
            if row.parent_id is not None:
                overrides = {
                    role: assignment.resource
                    for role, slot in row.template.resource_slots.items()
                    if (assignment := row.assignments.get(slot.id)) is not None
                }
            self._named(
                ref,
                "resource_overrides",
                overrides,
                {"template", "parent", "children", "properties"},
            )
        if "children" in self._coverage[ref] and "children" not in requested:
            for child in row.children:
                self._enqueue(child, requested)
