from __future__ import annotations

import warnings
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from recap.adapter.local import LocalBackend
from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.db.namespace import Namespace
from recap.db.process import ProcessRun, ProcessTemplate, ResourceSlot
from recap.db.resource import Resource, ResourceTemplate, ResourceType
from recap.db.step import StepTemplate, StepTemplateResourceSlotBinding
from recap.dsl.query import Field, QueryDSL, ResourceQuery
from recap.exceptions import UnloadedFieldError, UnloadedFieldWarning
from recap.lifecycle import LifecycleStatus
from recap.schemas.namespace import NamespaceContext
from recap.schemas.process import (
    ProcessRunSchema,
    ProcessTemplateSchema,
)
from recap.schemas.resource import (
    ResourceSchema,
    ResourceTemplateSchema,
)
from recap.utils.database import get_or_create
from recap.utils.general import Direction

_ACTIVE_CONTEXT = None


@pytest.fixture(autouse=True)
def active_namespace(db_session):
    global _ACTIVE_CONTEXT
    namespace = Namespace(
        id=uuid4(),
        path=f"test/{uuid4().hex}",
        metadata_json={},
        status=LifecycleStatus.ACTIVE,
    )
    db_session.add(namespace)
    db_session.flush()
    _ACTIVE_CONTEXT = NamespaceContext(id=namespace.id, path=namespace.path)

    def assign_namespace(_session, _flush_context, _instances):
        for item in _session.new:
            if isinstance(
                item, ProcessTemplate | ResourceTemplate | Resource | ProcessRun
            ):
                item.namespace = namespace
                item.status = LifecycleStatus.ACTIVE

    event.listen(db_session, "before_flush", assign_namespace)
    yield namespace
    event.remove(db_session, "before_flush", assign_namespace)


def make_query(db_session):
    SessionLocal = sessionmaker(bind=db_session.get_bind())
    backend = LocalBackend(SessionLocal)
    return QueryDSL(backend, context=_ACTIVE_CONTEXT)


def seed_process_run(
    db_session,
    *,
    name: str,
    with_parameters: bool = False,
    with_resource: bool = False,
    bind_step_slot: bool = False,
    with_resource_child: bool = False,
) -> tuple[Namespace, ProcessRun]:
    with db_session.no_autoflush:
        template = ProcessTemplate(name=f"Template-{name}", version="1.0")
        step_template = StepTemplate(name=f"Step-{name}", process_template=template)

        if with_parameters:
            attr = AttributeGroupTemplate(name=f"Exposure-{name}")
            attr.attribute_templates.append(
                AttributeTemplate(
                    name="dwell_time", value_type="int", default_value="5"
                )
            )
            step_template.attribute_group_templates.append(attr)
        run = ProcessRun(
            name=f"Run-{name}",
            description=f"Process run for {name}",
            template=template,
        )

    db_session.add_all([template, step_template])

    if with_resource:
        resource_type = ResourceType(name=f"resource-type-{uuid4().hex}")
        resource_template = ResourceTemplate(name=f"resource-template-{uuid4().hex}")
        resource_template.types.append(resource_type)
        slot = ResourceSlot(
            name=f"slot-{name}",
            process_template=template,
            resource_type=resource_type,
            direction=Direction.input,
        )
        resource = Resource(name=f"Resource-{name}", template=resource_template)
        if with_resource_child:
            Resource(
                name=f"Resource-{name}-child",
                template=resource_template,
                parent=resource,
            )
        db_session.add_all([resource_type, resource_template, slot, resource])
        if bind_step_slot:
            step_template.bindings["input_resource"] = StepTemplateResourceSlotBinding(
                role="input_resource",
                resource_slot=slot,
            )
    db_session.flush()
    db_session.add(run)
    if with_resource:
        run.resources[slot] = resource

    db_session.commit()
    return run.namespace, run


def test_process_run_pagination_and_filtering(db_session):
    runs = [seed_process_run(db_session, name=f"batch-{idx}")[1] for idx in range(3)]
    names = sorted(run.name for run in runs)

    query = (
        make_query(db_session)
        .process_runs()
        .where(Field("name").starts_with("Run-batch"))
        .order_by(Field("name"))
    )

    # head = query.limit(2).as_models()
    # assert [run.name for run in head] == names[:2]

    third = query.offset(2).first()
    assert third.name == names[2]

    filtered = query.where(Field("name") == names[1]).all()
    assert [run.name for run in filtered] == [names[1]]


def test_process_run_include_resources(db_session):
    _, run = seed_process_run(db_session, name="resources", with_resource=True)

    loaded_run = (
        make_query(db_session)
        .process_runs()
        .filter(id=run.id)
        .include_resources()
        .first()
    )

    assert loaded_run is not None
    assignment = next(iter(loaded_run.assigned_resources.values()))
    assert assignment.resource.name.startswith("Resource-resources")


def test_unloaded_process_run_field_warns_by_default(db_session):
    _, run = seed_process_run(db_session, name="warn-resources", with_resource=True)
    loaded_run = make_query(db_session).process_runs().filter(id=run.id).first()

    assert loaded_run is not None
    with pytest.warns(UnloadedFieldWarning, match="include\\('resources'\\)"):
        assert loaded_run.assigned_resources == {}


def test_unloaded_process_run_field_raises_when_configured(db_session):
    _, run = seed_process_run(db_session, name="raise-resources", with_resource=True)
    loaded_run = (
        make_query(db_session)
        .process_runs(on_unloaded="raise")
        .filter(id=run.id)
        .first()
    )

    assert loaded_run is not None
    with pytest.raises(UnloadedFieldError, match="include\\('resources'\\)"):
        _ = loaded_run.assigned_resources


def test_unloaded_resource_field_warns_by_default(db_session):
    _, run = seed_process_run(db_session, name="warn-properties", with_resource=True)
    resource = next(iter(run.resources.values()))
    loaded_resource = make_query(db_session).resources().filter(id=resource.id).first()

    assert loaded_resource is not None
    with pytest.warns(UnloadedFieldWarning, match="include\\('properties'\\)"):
        assert loaded_resource.properties == {}


def test_process_run_include_resources_populates_step_resources(db_session):
    _, run = seed_process_run(
        db_session,
        name="step-resources",
        with_resource=True,
        bind_step_slot=True,
        with_resource_child=True,
    )

    loaded_run = (
        make_query(db_session)
        .process_runs()
        .filter(id=run.id)
        .include_resources()
        .first()
    )

    assert loaded_run is not None
    step = loaded_run.steps["Step-step-resources"]
    assert "input_resource" in step.resources
    assert step.resources["input_resource"].name.startswith("Resource-step-resources")


def test_include_accepts_list_of_paths(db_session):
    query = (
        make_query(db_session)
        .process_runs()
        .include(["steps", "steps.parameters", "resources"])
    )
    assert query._spec.preloads == ["steps", "steps.parameters", "resources"]


def test_include_steps_with_parameters_adds_nested_preload(db_session):
    query = make_query(db_session).process_runs().include_steps(include_parameters=True)
    assert "steps" in query._spec.preloads
    assert "steps.parameters" in query._spec.preloads
    assert query._spec.load_mode == "none"


@pytest.mark.parametrize(
    ("factory", "schema"),
    [
        ("process_runs", ProcessRunSchema),
        ("process_templates", ProcessTemplateSchema),
        ("resources", ResourceSchema),
        ("resource_templates", ResourceTemplateSchema),
    ],
)
def test_query_defaults_use_full_shape(factory, schema, read_client):
    query = getattr(read_client.query_maker(), factory)()

    assert query._shape == "full"
    assert query.model is schema
    assert query._spec.load_mode == "none"


@pytest.mark.parametrize(
    ("factory", "schema"),
    [
        ("process_runs", ProcessRunSchema),
        ("process_templates", ProcessTemplateSchema),
        ("resources", ResourceSchema),
        ("resource_templates", ResourceTemplateSchema),
    ],
)
def test_query_accepts_eager_load(factory, schema, read_client):
    query = getattr(read_client.query_maker(), factory)(shape="full", load="eager")

    assert query._shape == "full"
    assert query._load == "eager"
    assert query.model is schema
    assert query._spec.load_mode == "eager"


def test_query_entity_and_load_parity(read_client):
    query = read_client.query_maker()

    resources = query.resources().filter(name="plate-1").all()
    templates = query.resource_templates().filter(name="Parity plate").all()
    runs = query.process_runs().filter(name="run-high").all()
    process_templates = query.process_templates().filter(name="Parity workflow").all()

    assert [type(item) for item in resources] == [ResourceSchema]
    assert [type(item) for item in templates] == [ResourceTemplateSchema]
    assert [type(item) for item in runs] == [ProcessRunSchema]
    assert [type(item) for item in process_templates] == [ProcessTemplateSchema]
    assert [item.name for item in resources] == ["plate-1"]
    assert [item.name for item in templates] == ["Parity plate"]
    assert [item.name for item in runs] == ["run-high"]
    assert [item.name for item in process_templates] == ["Parity workflow"]

    assert query.resources().filter(name="plate-1").count() == 1
    assert query.resources().filter(name="missing").first() is None


def test_query_relations_and_identity_are_local_remote_parity(read_client):
    query = read_client.query_maker()
    partial = query.resources().filter(name="plate-1").first()
    assert partial is not None
    assert partial.is_loaded("properties") is False

    direct_template = query.resource_templates().filter(name="Parity plate").first()
    loaded = query.resources().filter(name="plate-1").include_template().first()
    eager = query.resources(load="eager").filter(name="plate-1").first()
    run = query.process_runs().filter(name="run-high").include_resources().first()
    repeated = query.resources().filter(name="plate-1").first()

    assert loaded is partial
    assert loaded.template is direct_template
    assert run is not None
    assert next(iter(run.assigned_resources.values())).resource is loaded
    assert repeated is loaded

    assert eager is loaded
    assert eager.is_loaded("properties") is True


def test_deprecated_query_names_warn_and_normalize_immediately(db_session):
    with pytest.warns(DeprecationWarning) as warnings_seen:
        query = make_query(db_session).resources(shape="schema", load="full")

    assert len(warnings_seen) == 2
    assert query._shape == "full"
    assert query._load == "eager"
    assert query._spec.load_mode == "eager"


def test_cloning_normalized_query_emits_no_deprecation_warning(db_session):
    with pytest.warns(DeprecationWarning):
        query = make_query(db_session).resources(shape="schema", load="full")

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        cloned = query.filter(name="sample").limit(1)

    assert cloned._shape == "full"
    assert cloned._load == "eager"


def test_direct_query_construction_normalizes_deprecated_names(db_session):
    backend = make_query(db_session).backend
    with pytest.warns(DeprecationWarning) as warnings_seen:
        query = ResourceQuery(
            backend,
            context=make_query(db_session).context,
            shape="schema",
            load="full",
        )

    assert len(warnings_seen) == 2
    assert query._shape == "full"
    assert query._load == "eager"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"shape": "invalid"}, "shape must be one of"),
        ({"load": "invalid"}, "load must be one of"),
    ],
)
def test_query_rejects_unknown_shape_and_load(kwargs, message, db_session):
    with pytest.raises(ValueError, match=message):
        make_query(db_session).resources(**kwargs)


@pytest.mark.parametrize(
    "factory",
    ["process_runs", "process_templates", "resources", "resource_templates"],
)
def test_query_include_rejects_eager_load(factory, db_session):
    with pytest.raises(ValueError, match="load='eager'"):
        getattr(make_query(db_session), factory)(load="eager").include("relation")


def test_process_runs_ref_shape_and_load_mode(db_session):
    query = make_query(db_session).process_runs(shape="ref")
    assert query._spec.load_mode == "none"


def test_run_assignment_auto_populates_bound_step_assignments(db_session):
    _, run = seed_process_run(
        db_session,
        name="auto-step-assignment",
        with_resource=True,
        bind_step_slot=True,
    )

    loaded_run = (
        make_query(db_session)
        .process_runs()
        .filter(id=run.id)
        .include_steps(include_parameters=False)
        .include_resources()
        .first()
    )

    assert loaded_run is not None
    step = loaded_run.steps["Step-auto-step-assignment"]
    assert step.resources["input_resource"].name.startswith(
        "Resource-auto-step-assignment"
    )


def test_process_run_query_can_return_ref(db_session):
    _, run = seed_process_run(db_session, name="ref-run")

    ref = make_query(db_session).process_runs(shape="ref").filter(id=run.id).first()

    assert isinstance(ref, ProcessRunSchema)
    assert isinstance(ref.template, ProcessTemplateSchema)
    assert ref.is_loaded("steps") is False


def test_process_template_query_can_return_ref(db_session):
    _, run = seed_process_run(db_session, name="pt-ref")

    ref = (
        make_query(db_session)
        .process_templates(shape="ref")
        .filter(id=run.template.id)
        .first()
    )

    assert isinstance(ref, ProcessTemplateSchema)
    assert ref.step_templates == {}


def test_process_template_includes(db_session):
    _, run = seed_process_run(db_session, name="pt-include", with_resource=True)

    tmpl = (
        make_query(db_session)
        .process_templates()
        .filter(id=run.template.id)
        .include_step_templates()
        .include_resource_slots()
        .first()
    )

    assert tmpl is not None
    assert "Step-pt-include" in tmpl.step_templates
    assert any(rs.name.startswith("slot-pt-include") for rs in tmpl.resource_slots)
    assert tmpl.is_loaded("step_templates") is True
    assert tmpl.is_loaded("resource_slots") is True


def test_process_template_query_marks_unrequested_relations_unloaded(db_session):
    _, run = seed_process_run(db_session, name="pt-flags", with_resource=True)

    tmpl = make_query(db_session).process_templates().filter(id=run.template.id).first()

    assert tmpl is not None
    assert tmpl.is_loaded("step_templates") is False
    assert tmpl.is_loaded("resource_slots") is False


def test_resource_queries_can_return_refs(db_session):
    resource_type = ResourceType(name="rt")
    resource_template = ResourceTemplate(name="rtmpl", version="1.0")
    resource_template.types.append(resource_type)
    resource = Resource(name="res-ref", template=resource_template)
    db_session.add_all([resource_type, resource_template, resource])
    db_session.commit()

    res_ref = (
        make_query(db_session).resources(shape="ref").filter(id=resource.id).first()
    )
    tmpl_ref = (
        make_query(db_session)
        .resource_templates(shape="ref")
        .filter(id=resource_template.id)
        .first()
    )

    assert isinstance(res_ref, ResourceSchema)
    assert isinstance(res_ref.template, ResourceTemplateSchema)
    assert isinstance(tmpl_ref, ResourceTemplateSchema)


def test_resource_template_includes(db_session):
    resource_type = ResourceType(name="rt-inc")
    parent = ResourceTemplate(name="rt-parent", version="1.0")
    parent.types.append(resource_type)

    child = ResourceTemplate(name="rt-child", version="1.0", parent=parent)
    ag = AttributeGroupTemplate(name="Props-inc", resource_template=parent)
    ag.attribute_templates.append(
        AttributeTemplate(name="length", value_type="int", default_value="5")
    )

    db_session.add_all([resource_type, parent, child, ag])
    db_session.commit()

    tmpl = (
        make_query(db_session)
        .resource_templates()
        .filter(id=parent.id)
        .include_children()
        .include_attribute_groups()
        .include_types()
        .first()
    )

    assert tmpl is not None
    assert "rt-child" in tmpl.children
    assert any(
        at.name == "length"
        for at in tmpl.attribute_group_templates[0].attribute_templates
    )
    assert any(t.name == "rt-inc" for t in tmpl.types)
    assert tmpl.is_loaded("children") is True
    assert tmpl.is_loaded("attribute_group_templates") is True
    assert tmpl.is_loaded("types") is True
    assert tmpl.is_loaded("parent") is False


def test_eager_template_query_marks_all_relations_loaded(db_session):
    _, run = seed_process_run(db_session, name="pt-eager", with_resource=True)
    process_template = (
        make_query(db_session)
        .process_templates(load="eager")
        .filter(id=run.template.id)
        .first()
    )
    assert process_template is not None
    assert process_template.is_loaded("step_templates") is True
    assert process_template.is_loaded("resource_slots") is True


def test_resource_property_filtering_and_parent_scope(db_session):
    container_type, _ = get_or_create(
        db_session, ResourceType, where={"name": "container"}
    )
    parent_tmpl = ResourceTemplate(name="Parent", version="1.0")
    parent_tmpl.types.append(container_type)
    child_tmpl = ResourceTemplate(name="Child", version="1.0", parent=parent_tmpl)

    metrics = AttributeGroupTemplate(name="metrics", resource_template=child_tmpl)
    metrics.attribute_templates.append(
        AttributeTemplate(name="height", value_type="int", default_value="0")
    )
    metrics.attribute_templates.append(
        AttributeTemplate(name="label", value_type="str", default_value="")
    )

    db_session.add_all([container_type, parent_tmpl, child_tmpl, metrics])
    db_session.commit()

    parent_res = Resource(name="parent-res", template=parent_tmpl)
    child_short = Resource(name="child-short", template=child_tmpl, parent=parent_res)
    child_tall = Resource(name="child-tall", template=child_tmpl, parent=parent_res)

    child_short.properties["metrics"].values["height"] = 5
    child_tall.properties["metrics"].values["height"] = 15
    child_tall.properties["metrics"].values["label"] = "tall"

    db_session.add_all([parent_res, child_short, child_tall])
    db_session.commit()

    q = make_query(db_session).resources()

    tall = q.filter_property("height", gt=10, group="metrics").all()
    assert {r.name for r in tall} == {"child-tall"}

    scoped = q.filter_property("height", gt=10).under_parent(parent_res).all()
    assert {r.name for r in scoped} == {"child-tall"}
    assert all(r.name != "parent-res" for r in scoped)


def test_process_run_parameter_filtering(db_session):
    with db_session.no_autoflush:
        tmpl = ProcessTemplate(name="PT-param", version="1.0")
        step_tmpl = StepTemplate(name="Collect", process_template=tmpl)
        params_grp = AttributeGroupTemplate(name="Exposure", step_template=step_tmpl)
        params_grp.attribute_templates.append(
            AttributeTemplate(name="dwell", value_type="int", default_value="5")
        )

        db_session.add_all([tmpl, step_tmpl, params_grp])
        db_session.commit()

        run_low = ProcessRun(
            name="run-low",
            description="low dwell",
            template=tmpl,
        )
        run_high = ProcessRun(
            name="run-high",
            description="high dwell",
            template=tmpl,
        )

        run_low.steps["Collect"].parameters["Exposure"].values["dwell"] = 4
        run_high.steps["Collect"].parameters["Exposure"].values["dwell"] = 12

        db_session.add_all([run_low, run_high])
        db_session.commit()

    q = make_query(db_session).process_runs()

    hits = q.filter_parameter("dwell", gt=10, group="Exposure", step="Collect").all()
    assert {r.name for r in hits} == {"run-high"}

    # Group and step are optional when unambiguous
    hits2 = q.filter_parameter("dwell", gt=10).all()
    assert {r.name for r in hits2} == {"run-high"}


def test_process_run_parameter_filtering_strings(db_session):
    """filter_parameter(eq=...) must work for string-typed parameters.

    Regression: string values are stored JSON-encoded in the value_json column
    (e.g., '"active"'), so the comparison must encode the RHS the same way.
    """
    with db_session.no_autoflush:
        tmpl = ProcessTemplate(name="PT-str", version="1.0")
        step_tmpl = StepTemplate(name="Collect", process_template=tmpl)
        params_grp = AttributeGroupTemplate(name="Status", step_template=step_tmpl)
        params_grp.attribute_templates.append(
            AttributeTemplate(name="state", value_type="str", default_value="pending")
        )

        db_session.add_all([tmpl, step_tmpl, params_grp])
        db_session.commit()

        run_active = ProcessRun(
            name="run-active",
            description="active run",
            template=tmpl,
        )
        run_done = ProcessRun(
            name="run-done",
            description="done run",
            template=tmpl,
        )

        run_active.steps["Collect"].parameters["Status"].values["state"] = "active"
        run_done.steps["Collect"].parameters["Status"].values["state"] = "done"

        db_session.add_all([run_active, run_done])
        db_session.commit()

    q = make_query(db_session).process_runs()

    # eq with string value
    hits = q.filter_parameter("state", eq="active", group="Status").all()
    assert {r.name for r in hits} == {"run-active"}

    # in_ with string values
    hits_in = q.filter_parameter("state", in_=["active", "done"], group="Status").all()
    assert {r.name for r in hits_in} == {"run-active", "run-done"}

    # eq with a value that matches no rows
    hits_none = q.filter_parameter("state", eq="missing", group="Status").all()
    assert hits_none == []


def test_resource_property_filtering_strings(db_session):
    """filter_property(eq=...) must work for string-typed properties."""
    container_type, _ = get_or_create(
        db_session, ResourceType, where={"name": "container-str"}
    )
    parent_tmpl = ResourceTemplate(name="Parent-str", version="1.0")
    parent_tmpl.types.append(container_type)
    child_tmpl = ResourceTemplate(name="Child-str", version="1.0", parent=parent_tmpl)

    info = AttributeGroupTemplate(name="info", resource_template=child_tmpl)
    info.attribute_templates.append(
        AttributeTemplate(name="status", value_type="str", default_value="unknown")
    )

    db_session.add_all([container_type, parent_tmpl, child_tmpl, info])
    db_session.commit()

    parent_res = Resource(name="parent-str", template=parent_tmpl)
    child_a = Resource(name="child-a", template=child_tmpl, parent=parent_res)
    child_b = Resource(name="child-b", template=child_tmpl, parent=parent_res)

    child_a.properties["info"].values["status"] = "available"
    child_b.properties["info"].values["status"] = "reserved"

    db_session.add_all([parent_res, child_a, child_b])
    db_session.commit()

    q = make_query(db_session).resources()

    hits = q.filter_property("status", eq="available", group="info").all()
    assert {r.name for r in hits} == {"child-a"}

    hits_in = q.filter_property(
        "status", in_=["available", "reserved"], group="info"
    ).all()
    assert {r.name for r in hits_in} == {"child-a", "child-b"}


def test_parameter_filtering_numeric_unchanged(db_session):
    """Ensure the coercion refactor doesn't break numeric/bool filtering."""
    with db_session.no_autoflush:
        tmpl = ProcessTemplate(name="PT-num", version="1.0")
        step_tmpl = StepTemplate(name="Measure", process_template=tmpl)
        params_grp = AttributeGroupTemplate(name="Readings", step_template=step_tmpl)
        params_grp.attribute_templates.append(
            AttributeTemplate(name="count", value_type="int", default_value="0")
        )
        params_grp.attribute_templates.append(
            AttributeTemplate(name="enabled", value_type="bool", default_value="true")
        )

        db_session.add_all([tmpl, step_tmpl, params_grp])
        db_session.commit()

        run_a = ProcessRun(
            name="run-num-a",
            description="a",
            template=tmpl,
        )
        run_b = ProcessRun(
            name="run-num-b",
            description="b",
            template=tmpl,
        )

        run_a.steps["Measure"].parameters["Readings"].values["count"] = 10
        run_a.steps["Measure"].parameters["Readings"].values["enabled"] = True
        run_b.steps["Measure"].parameters["Readings"].values["count"] = 50
        run_b.steps["Measure"].parameters["Readings"].values["enabled"] = False

        db_session.add_all([run_a, run_b])
        db_session.commit()

    q = make_query(db_session).process_runs()

    hits_eq = q.filter_parameter("count", eq=50, group="Readings").all()
    assert {r.name for r in hits_eq} == {"run-num-b"}

    hits_gt = q.filter_parameter("count", gt=20, group="Readings").all()
    assert {r.name for r in hits_gt} == {"run-num-b"}

    hits_bool = q.filter_parameter("enabled", eq=True, group="Readings").all()
    assert {r.name for r in hits_bool} == {"run-num-a"}


def _seed_resource_hierarchy(db_session):
    """Build a 3-level resource tree spanning two templates.

    root (tmpl_a)
      ├─ mid-1 (tmpl_b)  └─ leaf-1 (tmpl_b)
      └─ mid-2 (tmpl_a)
    """
    tmpl_a = ResourceTemplate(name="Box", version="1.0")
    tmpl_b = ResourceTemplate(name="Vial", version="1.0")
    db_session.add_all([tmpl_a, tmpl_b])
    db_session.commit()

    root = Resource(name="root", template=tmpl_a)
    mid_1 = Resource(name="mid-1", template=tmpl_b, parent=root)
    mid_2 = Resource(name="mid-2", template=tmpl_a, parent=root)
    leaf_1 = Resource(name="leaf-1", template=tmpl_b, parent=mid_1)
    db_session.add_all([root, mid_1, mid_2, leaf_1])
    db_session.commit()

    return root, tmpl_a, tmpl_b


def test_descendants_matches_manual_chain(db_session):
    """descendants(parent) returns the same rows as the README-recommended
    under_parent().include([...]) chain it wraps."""
    root, _, _ = _seed_resource_hierarchy(db_session)

    manual = (
        make_query(db_session)
        .resources()
        .under_parent(root)
        .include(["template", "properties"])
        .all()
    )
    helper = make_query(db_session).resources().descendants(root).all()

    assert {r.id for r in helper} == {r.id for r in manual}
    # All descendants, every level -- not just direct children.
    assert {r.name for r in helper} == {"mid-1", "mid-2", "leaf-1"}


def test_descendants_of_template_filters_by_template(db_session):
    """descendants(parent, of_template=...) restricts to one resource template."""
    root, _, tmpl_b = _seed_resource_hierarchy(db_session)

    rows = (
        make_query(db_session)
        .resources()
        .descendants(root, of_template=tmpl_b.id)
        .all()
    )

    assert {r.name for r in rows} == {"mid-1", "leaf-1"}


def test_descendants_hydrates_template_and_properties(db_session):
    """The wrapped include([...]) eagerly loads template + properties so the
    returned schemas are usable without further round-trips."""
    root, _, _ = _seed_resource_hierarchy(db_session)

    rows = make_query(db_session).resources().descendants(root).all()

    for r in rows:
        assert r.template is not None
        # properties is a loaded (possibly empty) mapping, not an unloaded marker
        assert r.properties is not None
