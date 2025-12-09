import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload, sessionmaker

from recap.adapter.local import LocalBackend
from recap.db.step import Parameter, Step
from recap.dsl.process_builder import ProcessRunBuilder, ProcessTemplateBuilder


@pytest.fixture
def backend(apply_migrations, engine):
    SessionLocal = sessionmaker(bind=engine)
    backend = LocalBackend(SessionLocal)
    try:
        yield backend
    finally:
        backend.close()


def test_process_run_update_persists_param_changes(backend):
    uow = backend.begin()
    campaign = backend.create_campaign("Campaign", "proposal-1", saf=None)
    uow.commit()

    with ProcessTemplateBuilder(backend, "PT-update", "1.0") as ptb:
        (
            ptb.add_step("Mix")
            .param_group("Inputs")
            .add_attribute("Voltage", "int", "", "0")
            .close_group()
            .close_step()
        )

    with ProcessRunBuilder(
        name="run-update",
        description="desc",
        template_name="PT-update",
        version="1.0",
        campaign=campaign,
        backend=backend,
    ) as prb:
        run = prb.process_run
        step = run.steps[0]

        # mutate typed param values and persist
        step.parameters["Inputs"].values.voltage = 42
        run.update()

    uow_read = backend.begin()
    orm_step = (
        backend.session.execute(
            select(Step)
            .where(Step.id == step.id)
            .options(selectinload(Step.parameters).selectinload(Parameter._values))
        )
        .scalars()
        .one()
    )
    assert orm_step.parameters["Inputs"].values["Voltage"] == 42
    uow_read.rollback()


def test_resource_save_persists_property_changes(backend):
    uow = backend.begin()
    rt = backend.add_resource_types(["instrument"])[0]
    tmpl = backend.add_resource_template("Robot", [rt])
    details = backend.add_attr_group("Details", tmpl)
    backend.add_attribute("serial", "str", "", "abc", details)
    resource = backend.create_resource("R1", tmpl, None, expand=True)
    uow.commit()

    resource.properties["Details"].values.serial = "xyz"
    resource.save()

    uow_read = backend.begin()
    refreshed = backend.get_resource("R1", "Robot", expand=True)
    assert refreshed.properties["Details"].values.serial == "xyz"
    uow_read.rollback()
