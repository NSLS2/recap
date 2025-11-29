from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from recap.adapter.local import LocalBackend
from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.db.campaign import Campaign
from recap.db.process import Direction, ProcessRun, ProcessTemplate, ResourceSlot
from recap.db.resource import Resource, ResourceTemplate, ResourceType
from recap.db.step import StepTemplate
from recap.dsl.query import QueryDSL


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
    step = loaded_campaign.process_runs[0].steps[0]
    exposure = step.parameters["Exposure-include"]
    assert exposure.values["dwell_time"] == 5


def test_process_run_pagination_and_filtering(db_session):
    runs = [seed_process_run(db_session, name=f"batch-{idx}")[1] for idx in range(3)]
    names = sorted(run.name for run in runs)

    query = make_query(db_session).process_runs().order_by(ProcessRun.name)

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
