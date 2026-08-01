import pytest
from sqlalchemy.orm import sessionmaker

from recap.adapter.local import LocalBackend
from recap.db.namespace import Namespace
from recap.db.process import ProcessRun, ProcessTemplate, ResourceSlot
from recap.db.resource import Resource, ResourceTemplate, ResourceType
from recap.schemas.process import ProcessRunSchema
from recap.schemas.resource import ResourceSchema, ResourceSlotSchema
from recap.utils.general import Direction


@pytest.fixture
def backend(apply_migrations, engine):
    SessionLocal = sessionmaker(bind=engine)
    backend = LocalBackend(SessionLocal)
    uow = backend.begin()
    backend.namespace = Namespace(path="process-assignment", metadata_json={})
    backend.session.add(backend.namespace)
    try:
        yield backend
    finally:
        uow.rollback()
        backend.close()


def test_assign_resource_rejects_slot_from_other_template(backend):
    rt = ResourceType(name="rt")
    pt_run = ProcessTemplate(namespace=backend.namespace, name="PT-run", version="1")
    pt_slot = ProcessTemplate(namespace=backend.namespace, name="PT-slot", version="1")
    slot = ResourceSlot(
        name="slot-x",
        process_template=pt_slot,
        resource_type=rt,
        direction=Direction.input,
    )
    tmpl = ResourceTemplate(namespace=backend.namespace, name="RT", types=[rt])
    res = Resource(namespace=backend.namespace, name="R1", template=tmpl)
    run = ProcessRun(
        namespace=backend.namespace, name="run", description="", template=pt_run
    )

    backend.session.add_all([rt, pt_run, pt_slot, slot, tmpl, res, run])
    backend.session.flush()

    slot_schema = ResourceSlotSchema.model_validate(slot)
    res_schema = ResourceSchema.model_validate(res, from_attributes=True)
    run_schema = ProcessRunSchema.model_validate(run, from_attributes=True)

    with pytest.raises(ValueError, match="does not belong"):
        backend.assign_resource(slot_schema, res_schema, run_schema)


def test_assign_resource_prevents_duplicate_slot_usage(backend):
    rt = ResourceType(name="rt3")
    pt = ProcessTemplate(namespace=backend.namespace, name="PT3", version="1")
    slot = ResourceSlot(
        name="slot-z", process_template=pt, resource_type=rt, direction=Direction.input
    )
    tmpl = ResourceTemplate(namespace=backend.namespace, name="RT3", types=[rt])
    res1 = Resource(namespace=backend.namespace, name="R-active-1", template=tmpl)
    res2 = Resource(namespace=backend.namespace, name="R-active-2", template=tmpl)
    run = ProcessRun(
        namespace=backend.namespace, name="run3", description="", template=pt
    )

    backend.session.add_all([rt, pt, slot, tmpl, res1, res2, run])
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
