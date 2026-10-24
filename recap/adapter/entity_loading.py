"""Prepare requested ORM graphs in bulk before extracting flat records."""

from collections import defaultdict

from sqlalchemy import inspect, select
from sqlalchemy.orm import attributes, joinedload, selectinload

from recap.adapter.entity_batch import EntityBatchCollector
from recap.adapter.entity_records import POLICIES_BY_ORM
from recap.adapter.hydration_relations import index_children
from recap.adapter.query_loaders import property_loader_options, resolve_loader_options
from recap.db.attribute import AttributeGroupTemplate, AttributeValue
from recap.db.process import ProcessRun, ResourceAssignment, ResourceSlot
from recap.db.resource import Resource, ResourceTemplate
from recap.db.step import Parameter, Step, StepTemplate, StepTemplateResourceSlotBinding
from recap.schemas.process import ProcessRunSchema, ProcessTemplateSchema
from recap.schemas.step import StepTemplateSchema
from recap.utils.loaders import chain_load


def _chunks(values):
    values = list(dict.fromkeys(values))
    for start in range(0, len(values), 500):
        yield values[start : start + 500]


def _fetch(session, model, ids, options):
    rows = []
    for chunk in _chunks(ids):
        rows.extend(
            session.scalars(
                select(model).where(model.id.in_(chunk)).options(*options)
            ).unique()
        )
    return rows


def _load_missing(session, rows, required, options):
    ids = [
        row.id
        for row in rows
        if required & inspect(row).unloaded or inspect(row).expired_attributes
    ]
    if ids:
        _fetch(session, type(rows[0]), ids, options)


def _tree_rows(session, model, roots, options, *, ancestors=False):
    """Load recursive closure with bounded ID batches, independent of tree depth."""
    ids = []
    for chunk in _chunks(roots):
        tree = (
            select(model.id, model.parent_id)
            .where(model.id.in_(chunk))
            .cte(recursive=True)
        )
        join = (
            model.id == tree.c.parent_id if ancestors else model.parent_id == tree.c.id
        )
        tree = tree.union(select(model.id, model.parent_id).join(tree, join))
        ids.extend(session.scalars(select(tree.c.id)))
    if options:
        return _fetch(session, model, ids, options)
    # Closure CTEs already selected identities. Reuse attached scalar rows instead
    # of selecting their columns again merely to populate a flat child index.
    known = {}
    for object_id in ids:
        row = session.identity_map.get(session.identity_key(model, (object_id,)))
        if row is not None and not inspect(row).expired_attributes:
            known[object_id] = row
    known.update(
        {
            row.id: row
            for row in _fetch(
                session,
                model,
                [object_id for object_id in ids if object_id not in known],
                [],
            )
        }
    )
    return [known[object_id] for object_id in dict.fromkeys(ids)]


def _resource_options(requested):
    options = [joinedload(Resource.template)]
    if "parent" in requested:
        options.append(joinedload(Resource.parent).joinedload(Resource.template))
    if "properties" in requested:
        options.extend(property_loader_options())
    return options


def _template_options(requested):
    options = []
    if "parent" in requested:
        options.append(joinedload(ResourceTemplate.parent))
    if "types" in requested:
        options.append(selectinload(ResourceTemplate.types))
    if "attribute_group_templates" in requested:
        options.append(
            chain_load(
                ResourceTemplate.attribute_group_templates,
                AttributeGroupTemplate.attribute_templates,
            )
        )
    return options


class _GraphPreparation(EntityBatchCollector):
    """Walk the collector's coverage rules without constructing records or edges.

    Loading lives here; the actual collector remains an extraction-only pass.
    Using the same expansion handlers also preserves projection upgrades reached
    through multiple occurrences of an identity.
    """

    def __init__(self, session):
        super().__init__(children_map={})
        self.session = session

    def _single(self, source, relation, row, requested=frozenset(), *, loaded=True):
        if row is not None:
            self._enqueue(row, requested)

    def _named(self, source, relation, rows, requested=frozenset()):
        for row in rows.values():
            self._enqueue(row, requested)

    def _sequence(self, source, relation, rows, requested=frozenset()):
        for row in rows:
            self._enqueue(row, requested)

    def prepare(self):
        while self._queue:
            pending = list(self._queue)
            self._queue.clear()
            groups = defaultdict(dict)
            for row, ref, requested in pending:
                coverage = groups[ref.kind].setdefault(row, set())
                coverage.update(requested | self._coverage.get(ref, set()))
            for kind, rows in groups.items():
                by_coverage = defaultdict(list)
                for row, requested in rows.items():
                    by_coverage[frozenset(requested)].append(row)
                for requested, members in by_coverage.items():
                    getattr(self, f"_prepare_{kind}", self._prepare_scalars)(
                        members, set(requested)
                    )
            for row, ref, requested in pending:
                if ref not in self._records:
                    self._records[ref] = None
                    self._coverage[ref] = set()
                    self._supporting_relations(row, ref)
                new = requested - self._coverage[ref]
                if new:
                    self._coverage[ref].update(new)
                    getattr(self, f"_expand_{ref.kind}")(row, ref, new)

    def _prepare_scalars(self, rows, requested):
        _load_missing(self.session, rows, set(), [])

    def _prepare_resource(self, rows, requested):
        required = {"template"} | (requested & {"parent", "properties"})
        options = _resource_options(requested)
        if "children" in requested:
            missing = [row for row in rows if row.id not in self.children_map]
            if missing:
                flat = _tree_rows(
                    self.session, Resource, [row.id for row in missing], options
                )
                indexed = index_children(flat)
                self.children_map.update(
                    {row.id: indexed.get(row.id, []) for row in flat}
                )
                rows = list(dict.fromkeys([*rows, *flat]))
        _load_missing(self.session, rows, required, options)
        if "template" in requested:
            templates = [row.template for row in rows]
            if "parent" in requested:
                templates.extend(
                    row.parent.template for row in rows if row.parent is not None
                )
            self._prepare_resource_template(
                list(dict.fromkeys(templates)),
                {"types", "parent", "children", "attribute_group_templates"},
            )

    def _prepare_resource_template(self, rows, requested):
        _load_missing(
            self.session, rows, requested - {"children"}, _template_options(requested)
        )
        flat = rows
        if "children" in requested:
            missing = [row for row in rows if "children" in inspect(row).unloaded]
            if missing:
                # Descendants inherit full supporting template coverage. Query
                # roots retain their own requested attribute-group coverage.
                flat = _tree_rows(
                    self.session, ResourceTemplate, [row.id for row in missing], []
                )
                descendants = [row for row in flat if row not in rows]
                full = {"parent", "types", "attribute_group_templates"}
                _load_missing(self.session, descendants, full, _template_options(full))
                indexed = index_children(flat)
                for row in flat:
                    if "children" in inspect(row).unloaded:
                        attributes.set_committed_value(
                            row, "children", indexed.get(row.id, [])
                        )
        if "parent" in requested or len(flat) > len(rows):
            missing = [
                row
                for row in flat
                if "parent" in inspect(row).unloaded
                or row.parent is not None
                and ({"parent", "types"} & inspect(row.parent).unloaded)
            ]
            if missing:
                _tree_rows(
                    self.session,
                    ResourceTemplate,
                    [row.id for row in missing],
                    _template_options({"parent", "types"}),
                    ancestors=True,
                )

    def _prepare_process_run(self, rows, requested):
        preloads = requested - {"assigned_resources", "resources"}
        if requested & {"resources", "steps.parameters"}:
            preloads = preloads | {"steps"}
        options = [
            selectinload(ProcessRun.template),
            *resolve_loader_options(ProcessRunSchema, sorted(preloads), None),
        ]
        required = {"template"}
        if requested & {"steps", "steps.parameters", "resources"}:
            required.add("steps")
        if requested & {"resources", "assigned_resources"}:
            required.add("assignments")
            # Assigned trees are prepared together by resource coverage traversal;
            # this phase only needs references and dictionary-key dependencies.
            options.extend(
                [
                    chain_load(
                        ProcessRun.assignments,
                        ResourceAssignment.resource,
                        Resource.template,
                        joined=(ResourceAssignment.resource, Resource.template),
                    ),
                    chain_load(
                        ProcessRun.assignments,
                        ResourceAssignment.resource_slot,
                        ResourceSlot.resource_type,
                        joined=(
                            ResourceAssignment.resource_slot,
                            ResourceSlot.resource_type,
                        ),
                    ),
                ]
            )
        _load_missing(self.session, rows, required, options)

    def _prepare_process_template(self, rows, requested):
        _load_missing(
            self.session,
            rows,
            requested,
            resolve_loader_options(ProcessTemplateSchema, sorted(requested), None),
        )

    def _prepare_step(self, rows, requested):
        required = {"template"} | (requested & {"children", "parameters"})
        options = [joinedload(Step.template)]
        if "children" in requested:
            options.append(selectinload(Step.children))
        if "parameters" in requested:
            options.extend(
                [
                    chain_load(
                        Step.parameters,
                        Parameter.template,
                        AttributeGroupTemplate.attribute_templates,
                        joined=(Parameter.template,),
                    ),
                    chain_load(
                        Step.parameters,
                        Parameter._values,
                        AttributeValue.template,
                        joined=(AttributeValue.template,),
                    ),
                ]
            )
        if requested & {"resources", "resource_overrides"}:
            required.update({"process_run", "assignments"})
            options.extend(
                [
                    joinedload(Step.process_run),
                    chain_load(
                        Step.assignments,
                        ResourceAssignment.resource_slot,
                        joined=(ResourceAssignment.resource_slot,),
                    ),
                    chain_load(Step.assignments, ResourceAssignment.resource),
                ]
            )
        _load_missing(self.session, rows, required, options)
        if requested & {"resources", "resource_overrides"}:
            self._prepare_step_template(
                list(dict.fromkeys(row.template for row in rows)), {"resource_slots"}
            )
            runs = list(
                dict.fromkeys(
                    row.process_run for row in rows if row.process_run is not None
                )
            )
            self._prepare_process_run(runs, {"assigned_resources"})

    def _prepare_step_template(self, rows, requested):
        required = requested - {"resource_slots"}
        options = []
        if "attribute_group_templates" in requested:
            options.append(
                chain_load(
                    StepTemplate.attribute_group_templates,
                    AttributeGroupTemplate.attribute_templates,
                )
            )
        if "resource_slots" in requested:
            required.add("bindings")
            options.append(
                chain_load(
                    StepTemplate.bindings,
                    StepTemplateResourceSlotBinding.resource_slot,
                    ResourceSlot.resource_type,
                    joined=(
                        StepTemplateResourceSlotBinding.resource_slot,
                        ResourceSlot.resource_type,
                    ),
                )
            )
        _load_missing(self.session, rows, required, options)

    def _prepare_property(self, rows, requested):
        model = type(rows[0])
        required = requested - {"values"}
        options = []
        if "template" in requested:
            options.append(
                chain_load(
                    model.template,
                    AttributeGroupTemplate.attribute_templates,
                    joined=(model.template,),
                )
            )
        if "values" in requested:
            required.add("_values")
            options.append(
                chain_load(
                    model._values,
                    AttributeValue.template,
                    joined=(AttributeValue.template,),
                )
            )
        _load_missing(self.session, rows, required, options)

    _prepare_parameter = _prepare_property

    def _prepare_attribute_group_template(self, rows, requested):
        _load_missing(
            self.session,
            rows,
            requested,
            [selectinload(AttributeGroupTemplate.attribute_templates)],
        )

    def _prepare_attribute_value(self, rows, requested):
        _load_missing(
            self.session, rows, {"template"}, [joinedload(AttributeValue.template)]
        )

    def _prepare_resource_slot(self, rows, requested):
        _load_missing(
            self.session,
            rows,
            {"resource_type"},
            [joinedload(ResourceSlot.resource_type)],
        )

    def _prepare_resource_assignment(self, rows, requested):
        required = requested - {"slot", "resource"}
        options = []
        if "slot" in requested:
            required.add("resource_slot")
            options.append(
                joinedload(ResourceAssignment.resource_slot).joinedload(
                    ResourceSlot.resource_type
                )
            )
        if "resource" in requested:
            required.add("resource")
            options.append(
                joinedload(ResourceAssignment.resource).joinedload(Resource.template)
            )
        _load_missing(self.session, rows, required, options)


def prepare_graph_rows(session, rows, schema, requested):
    """Load requested/supporting ORM coverage without constructing response records."""
    rows = list(rows)
    if any(POLICIES_BY_ORM[type(row)].schema is not schema for row in rows):
        raise ValueError("Graph root schema does not match ORM rows")
    preparation = _GraphPreparation(session)
    for row in rows:
        preparation.add_root(row, requested)
    preparation.prepare()
    return preparation.children_map


def load_resource_ancestors(session, root_ids):
    """Load validation lineage in bounded batches without expanding properties."""
    return _tree_rows(
        session, Resource, root_ids, [joinedload(Resource.parent)], ancestors=True
    )


def prepare_lifecycle_links(session, row, *, activating=False):
    """Prepare aggregate ownership and activation references before flush guards."""
    if isinstance(row, Resource) and row.parent_id is not None:
        load_resource_ancestors(session, [row.id])
    if isinstance(row, ProcessRun) and activating:
        prepare_run_links(session, row)


def _step_link_options(missing):
    options = []
    if "template" in missing:
        options.append(joinedload(Step.template))
    if "children" in missing:
        options.append(selectinload(Step.children))
    if "assignments" in missing:
        options.append(selectinload(Step.assignments))
    if "parameters" in missing:
        options.extend(
            [
                chain_load(
                    Step.parameters, Parameter.template, joined=(Parameter.template,)
                ),
                chain_load(
                    Step.parameters,
                    Parameter._values,
                    AttributeValue.template,
                    joined=(AttributeValue.template,),
                ),
            ]
        )
    return options


def prepare_run_links(session, run, *, template_slots=False):
    """Prepare missing validation links without reloading resource contents."""
    missing = {"template", "steps", "assignments"} & inspect(run).unloaded
    options = []
    if "template" in missing:
        options.append(joinedload(ProcessRun.template))
    if "steps" in missing:
        options.append(selectinload(ProcessRun.steps).joinedload(Step.template))
    if "assignments" in missing:
        options.extend(
            [
                chain_load(
                    ProcessRun.assignments,
                    ResourceAssignment.resource_slot,
                    ResourceSlot.resource_type,
                    joined=(
                        ResourceAssignment.resource_slot,
                        ResourceSlot.resource_type,
                    ),
                ),
                chain_load(
                    ProcessRun.assignments,
                    ResourceAssignment.resource,
                    Resource.template,
                    joined=(ResourceAssignment.resource, Resource.template),
                ),
            ]
        )
    _load_missing(session, [run], missing, options)
    if template_slots:
        prepare_graph_rows(
            session, [run.template], ProcessTemplateSchema, {"resource_slots"}
        )
    prepare_graph_rows(
        session,
        list(dict.fromkeys(step.template for step in run.steps.values())),
        StepTemplateSchema,
        {"attribute_group_templates", "resource_slots"},
    )
    groups = defaultdict(list)
    for step in run.steps.values():
        missing = {"template", "children", "parameters", "assignments"} & inspect(
            step
        ).unloaded
        groups[frozenset(missing)].append(step)
    for missing, steps in groups.items():
        _load_missing(session, steps, set(missing), _step_link_options(missing))


def prepare_batch_graph(session, rows, schema, requested, on_unloaded="warn"):
    """Prepare the same requested/supporting closure used by the batch collector."""
    rows = list(rows)
    children_map = prepare_graph_rows(session, rows, schema, requested)
    collector = EntityBatchCollector(children_map=children_map, on_unloaded=on_unloaded)
    for row in rows:
        collector.add_root(row, requested)
    return collector.collect()


def load_resource_subtrees(session, root_ids, requested):
    """Return attached flat resource rows through the shared preparation path."""
    flat = _tree_rows(session, Resource, root_ids, _resource_options(requested))
    preparation = _GraphPreparation(session)
    indexed = index_children(flat)
    preparation.children_map.update({row.id: indexed.get(row.id, []) for row in flat})
    preparation._prepare_resource(flat, requested)
    return flat
