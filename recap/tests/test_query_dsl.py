from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from recap.adapter.local import LocalBackend
from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.db.campaign import Campaign
from recap.db.process import ProcessRun, ProcessTemplate, ResourceSlot
from recap.db.resource import Resource, ResourceTemplate, ResourceType
from recap.db.step import StepTemplate
from recap.dsl.query import QueryDSL
from recap.schemas.process import ProcessRunRef, ProcessTemplateRef
from recap.schemas.resource import ResourceRef, ResourceTemplateRef
from recap.utils.general import Direction


def make_query(db_session):
    SessionLocal = sessionmaker(bind=db_session.get_bind())
    backend = LocalBackend(SessionLocal)
    return QueryDSL(backend)


def seed_process_run(
    db_session,
    *,
    name: str,
    with_parameters: bool = False,
    with_resource: bool = False,
) -> tuple[Campaign, ProcessRun]:
    with db_session.no_autoflush:
        campaign = Campaign(name=f"Campaign-{name}", proposal=f"PROP-{name}")
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
            campaign=campaign,
        )

    db_session.add_all([campaign, template, step_template])
    db_session.flush()

    db_session.add(run)
    db_session.flush()

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
        db_session.add_all([resource_type, resource_template, slot, resource])
        db_session.flush()
        run.resources[slot] = resource

    db_session.commit()
    return campaign, run


def test_campaign_without_include_requires_lazy_load(db_session):
    campaign, _ = seed_process_run(db_session, name="lazy")
    campaign_row = make_query(db_session).campaigns().filter(id=campaign.id).first()
    assert campaign_row is not None
    # with pytest.raises(DetachedInstanceError):
    _ = len(campaign_row.process_runs)


def test_campaign_include_process_runs_and_steps(db_session):
    campaign, _ = seed_process_run(db_session, name="include", with_parameters=True)

    loaded_campaign = (
        make_query(db_session)
        .campaigns()
        .filter(id=campaign.id)
        .include_process_runs()  # lambda q: q.include_steps(include_parameters=True))
        .first()
    )
    assert loaded_campaign is not None
    assert loaded_campaign.process_runs[0].name.startswith("Run-include")
    step = loaded_campaign.process_runs[0].steps["Step-include"]
    exposure = step.parameters["Exposure-include"]
    assert exposure.values.dwell_time == 5


def test_process_run_pagination_and_filtering(db_session):
    runs = [seed_process_run(db_session, name=f"batch-{idx}")[1] for idx in range(3)]
    names = sorted(run.name for run in runs)

    query = (
        make_query(db_session)
        .process_runs()
        .where(ProcessRun.name.like("Run-batch%"))
        .order_by(ProcessRun.name)
    )

    # head = query.limit(2).as_models()
    # assert [run.name for run in head] == names[:2]

    third = query.offset(2).first()
    assert third.name == names[2]

    filtered = query.where(ProcessRun.name == names[1]).all()
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
    assignment = next(iter(loaded_run.assigned_resources))
    assert assignment.resource.name.startswith("Resource-resources")


def test_process_run_query_can_return_ref(db_session):
    _, run = seed_process_run(db_session, name="ref-run")

    ref = make_query(db_session).process_runs(expand=False).filter(id=run.id).first()

    assert isinstance(ref, ProcessRunRef)
    assert isinstance(ref.template, ProcessTemplateRef)
    # Ref objects should not expose steps
    assert not hasattr(ref, "steps")


def test_process_template_query_can_return_ref(db_session):
    _, run = seed_process_run(db_session, name="pt-ref")

    ref = (
        make_query(db_session)
        .process_templates(expand=False)
        .filter(id=run.template.id)
        .first()
    )

    assert isinstance(ref, ProcessTemplateRef)
    assert not hasattr(ref, "step_templates")


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


def test_resource_queries_can_return_refs(db_session):
    resource_type = ResourceType(name="rt")
    resource_template = ResourceTemplate(name="rtmpl", version="1.0")
    resource_template.types.append(resource_type)
    resource = Resource(name="res-ref", template=resource_template)
    db_session.add_all([resource_type, resource_template, resource])
    db_session.commit()

    res_ref = (
        make_query(db_session).resources(expand=False).filter(id=resource.id).first()
    )
    tmpl_ref = (
        make_query(db_session)
        .resource_templates(expand=False)
        .filter(id=resource_template.id)
        .first()
    )

    assert isinstance(res_ref, ResourceRef)
    assert isinstance(res_ref.template, ResourceTemplateRef)
    assert isinstance(tmpl_ref, ResourceTemplateRef)


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


def test_resource_property_filtering_and_parent_scope(db_session):
    container_type = ResourceType(name="container")
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
        campaign = Campaign(name="C-param", proposal="P-param")
        tmpl = ProcessTemplate(name="PT-param", version="1.0")
        step_tmpl = StepTemplate(name="Collect", process_template=tmpl)
        params_grp = AttributeGroupTemplate(name="Exposure", step_template=step_tmpl)
        params_grp.attribute_templates.append(
            AttributeTemplate(name="dwell", value_type="int", default_value="5")
        )

        db_session.add_all([campaign, tmpl, step_tmpl, params_grp])
        db_session.commit()

        run_low = ProcessRun(
            name="run-low",
            description="low dwell",
            template=tmpl,
            campaign=campaign,
        )
        run_high = ProcessRun(
            name="run-high",
            description="high dwell",
            template=tmpl,
            campaign=campaign,
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
