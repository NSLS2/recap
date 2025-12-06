import pytest
from sqlalchemy.orm import sessionmaker

from recap.adapter.local import LocalBackend
from recap.db.campaign import Campaign
from recap.db.process import Direction, ProcessRun, ProcessTemplate, ResourceSlot
from recap.db.resource import Resource, ResourceTemplate, ResourceType
from recap.schemas.process import ProcessRunSchema
from recap.schemas.resource import ResourceSchema, ResourceSlotSchema


@pytest.fixture
def backend(apply_migrations, engine):
    SessionLocal = sessionmaker(bind=engine)
    backend = LocalBackend(SessionLocal)
    uow = backend.begin()
    try:
        yield backend
    finally:
        uow.rollback()
        backend.close()


def test_assign_resource_rejects_slot_from_other_template(backend):
    rt = ResourceType(name="rt")
    pt_run = ProcessTemplate(name="PT-run", version="1")
    pt_slot = ProcessTemplate(name="PT-slot", version="1")
    slot = ResourceSlot(
        name="slot-x",
        process_template=pt_slot,
        resource_type=rt,
        direction=Direction.input,
    )
    tmpl = ResourceTemplate(name="RT", types=[rt])
    res = Resource(name="R1", template=tmpl)
    camp = Campaign(name="C", proposal="p", saf=None, meta_data=None)
    run = ProcessRun(name="run", description="", template=pt_run, campaign=camp)

    backend.session.add_all([rt, pt_run, pt_slot, slot, tmpl, res, camp, run])
    backend.session.flush()

    slot_schema = ResourceSlotSchema.model_validate(slot)
    res_schema = ResourceSchema.model_validate(res, from_attributes=True)
    run_schema = ProcessRunSchema.model_validate(run, from_attributes=True)

    with pytest.raises(ValueError, match="does not belong"):
        backend.assign_resource(slot_schema, res_schema, run_schema)


def test_assign_resource_rejects_inactive_resource(backend):
    rt = ResourceType(name="rt2")
    pt = ProcessTemplate(name="PT", version="1")
    slot = ResourceSlot(
        name="slot-y", process_template=pt, resource_type=rt, direction=Direction.input
    )
    tmpl = ResourceTemplate(name="RT2", types=[rt])
    res = Resource(name="R-inactive", template=tmpl, active=False)
    camp = Campaign(name="C2", proposal="p2", saf=None, meta_data=None)
    run = ProcessRun(name="run2", description="", template=pt, campaign=camp)

    backend.session.add_all([rt, pt, slot, tmpl, res, camp, run])
    backend.session.flush()

    slot_schema = ResourceSlotSchema.model_validate(slot)
    res_schema = ResourceSchema.model_validate(res, from_attributes=True)
    run_schema = ProcessRunSchema.model_validate(run, from_attributes=True)

    with pytest.raises(ValueError, match="inactive"):
        backend.assign_resource(slot_schema, res_schema, run_schema)


def test_assign_resource_prevents_duplicate_slot_usage(backend):
    rt = ResourceType(name="rt3")
    pt = ProcessTemplate(name="PT3", version="1")
    slot = ResourceSlot(
        name="slot-z", process_template=pt, resource_type=rt, direction=Direction.input
    )
    tmpl = ResourceTemplate(name="RT3", types=[rt])
    res1 = Resource(name="R-active-1", template=tmpl)
    res2 = Resource(name="R-active-2", template=tmpl)
    camp = Campaign(name="C3", proposal="p3", saf=None, meta_data=None)
    run = ProcessRun(name="run3", description="", template=pt, campaign=camp)

    backend.session.add_all([rt, pt, slot, tmpl, res1, res2, camp, run])
    backend.session.flush()

    slot_schema = ResourceSlotSchema.model_validate(slot)
    run_schema = ProcessRunSchema.model_validate(run, from_attributes=True)

    run_schema = backend.assign_resource(
        slot_schema,
        ResourceSchema.model_validate(res1, from_attributes=True),
        run_schema,
    )

    with pytest.raises(ValueError, match="already assigned"):
        backend.assign_resource(
            slot_schema,
            ResourceSchema.model_validate(res2, from_attributes=True),
            run_schema,
        )
