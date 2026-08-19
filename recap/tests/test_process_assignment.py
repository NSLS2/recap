from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from recap.db.namespace import Namespace
from recap.db.process import ProcessRun, ProcessTemplate, ResourceSlot
from recap.db.resource import Resource, ResourceTemplate, ResourceType
from recap.utils.general import Direction


@pytest.fixture
def backend(apply_migrations, engine):
    SessionLocal = sessionmaker(bind=engine)
    connection = engine.connect()
    transaction = connection.begin()
    session = SessionLocal(bind=connection)
    namespace = Namespace(path=f"process-assignment-{uuid4()}", metadata_json={})
    session.add(namespace)
    session.flush()
    try:
        yield SimpleNamespace(session=session, namespace=namespace)
    finally:
        if transaction.is_active:
            transaction.rollback()
        session.close()
        connection.close()


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
    with pytest.raises(ValueError, match="does not belong to process template"):
        run.resources[slot] = res


def test_assign_resource_prevents_duplicate_slot_usage(client):
    with client.build_process_template("PT3", "1") as pt:
        pt.add_resource_slot(
            "slot-z", "container", Direction.input, create_resource_type=True
        )
    with client.build_resource_template(name="RT3", type_names=["container"]):
        pass
    res1 = client.create_resource("R-active-1", "RT3")
    res2 = client.create_resource("R-active-2", "RT3")
    with client.build_process_run("run3", "", "PT3", "1") as builder:
        builder.assign_resource("slot-z", res1)
        run_id = builder.process_run.id

    with pytest.raises(ValueError, match="already occupied"), client.build_process_run(
        process_run_id=run_id
    ) as builder:
            builder.assign_resource("slot-z", res2)
