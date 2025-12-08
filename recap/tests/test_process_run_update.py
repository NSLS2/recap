import pytest
from sqlalchemy.orm import sessionmaker

from recap.adapter.local import LocalBackend
from recap.db.step import Step
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

        orm_step = backend.session.get(Step, step.id)
        assert orm_step.parameters["Inputs"].values["Voltage"] == 42
