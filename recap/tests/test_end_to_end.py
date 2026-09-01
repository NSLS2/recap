"""End-to-end public API workflow for local and REST deployments."""

import pytest

from recap.client import RecapClient
from recap.lifecycle import LifecycleStatus
from recap.utils.general import Direction


@pytest.fixture(params=["local", "remote"], ids=["local", "rest"])
def end_to_end_client(request, tmp_path, rest_loopback_client):
    if request.param == "local":
        with RecapClient.from_sqlite(tmp_path / "end-to-end.db") as client:
            yield client
        return

    yield rest_loopback_client


@pytest.mark.integration
def test_complete_recap_workflow(end_to_end_client):
    """Create, execute, and query a complete provenance workflow."""
    client = end_to_end_client
    client.create_namespace("facility")
    scoped = client.namespace("facility/beamline")
    scoped.create_namespace("facility/beamline")

    namespace = scoped.update_namespace(
        metadata={"beamline": "AMX", "proposal": "E2E-001"}
    )
    assert namespace.metadata == {"beamline": "AMX", "proposal": "E2E-001"}

    with scoped.build_resource_template(
        name="E2E plate", type_names=["container", "plate"]
    ) as resource_template:
        resource_template.add_properties(
            {"metrics": [{"name": "rating", "type": "int", "default": 1}]}
        )
    resource_template.activate()

    plate = scoped.create_resource("plate-001", "E2E plate")
    with scoped.build_resource(resource_id=plate.id) as resource_builder:
        resource = resource_builder.get_model()
        resource.properties.metrics.rating = 12
        resource_builder.set_model(resource)
    resource_builder.finalize()

    with scoped.build_process_template("E2E workflow", "1.0") as process_template:
        process_template.add_resource_slot("plate", "container", Direction.input)
        (
            process_template.add_step("Collect")
            .add_parameters(
                {"exposure": [{"name": "dwell", "type": "int", "default": 1}]}
            )
            .bind_slot("source", "plate")
            .close_step()
        )
    process_template.activate()

    with scoped.build_process_run(
        "run-001", "End-to-end collection", "E2E workflow", "1.0"
    ) as process_run:
        process_run.assign_resource("plate", plate)
        parameters = process_run.get_params("Collect")
        parameters.exposure.dwell = 15
        process_run.set_params(parameters)
    process_run.finalize()

    query = scoped.query_maker()
    loaded_plate = query.resources(load="eager").filter(name="plate-001").first()
    loaded_run = query.process_runs(load="eager").filter(name="run-001").first()

    assert loaded_plate is not None
    assert loaded_plate.status is LifecycleStatus.ACTIVE
    assert loaded_plate.properties.metrics.rating.value == 12
    assert loaded_run is not None
    assert loaded_run.status is LifecycleStatus.ACTIVE
    assert loaded_run.steps["Collect"].parameters.exposure.dwell.value == 15
    assert loaded_run.assigned_resources["plate"].resource is loaded_plate
    assert query.resources().filter(name="plate-001").count() == 1
    assert query.process_runs().filter(name="run-001").count() == 1
