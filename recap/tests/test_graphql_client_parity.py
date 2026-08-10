from contextlib import ExitStack, nullcontext

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from recap.client import RecapClient
from recap.db.process import ProcessRun, ProcessTemplate
from recap.db.resource import Resource, ResourceTemplate
from recap.dsl.query import Field
from recap.exceptions import UnloadedFieldError, UnloadedFieldWarning
from recap.lifecycle import LifecycleStatus
from recap.schemas.process import ProcessRunRef, ProcessTemplateRef
from recap.schemas.resource import ResourceRef, ResourceTemplateRef
from recap.server.app import create_app
from recap.utils.general import Direction


def _public_dump(value):
    if isinstance(value, list | tuple):
        return [_public_dump(item) for item in value]
    if isinstance(value, dict):
        return {key: _public_dump(item) for key, item in value.items()}
    if not hasattr(value, "model_dump"):
        return value
    return value.model_dump(mode="json", by_alias=True)


def _assert_query_parity(clients, query):
    local, remote = clients
    namespace_path = local.namespace_context.path
    local_result = query(local.namespace(namespace_path).query_maker())
    remote_result = query(remote.namespace(namespace_path).query_maker())
    assert _public_dump(remote_result) == _public_dump(local_result)
    return local_result, remote_result


@pytest.fixture
def parity_clients(tmp_path, monkeypatch):
    db_path = tmp_path / "parity.db"
    with ExitStack() as stack:
        local = stack.enter_context(RecapClient.from_sqlite(db_path))

        local.create_namespace("test/mx-parity", metadata={"beamline": "AMX"})
        with local.build_resource_template(
            name="Parity plate", type_names=["container", "plate"]
        ) as template:
            template.add_properties(
                {"metrics": [{"name": "rating", "type": "int", "default": 1}]}
            )
            (
                template.add_child("sample", ["sample"])
                .add_properties(
                    {"contents": [{"name": "mass", "type": "float", "default": 2.5}]}
                )
                .close_child()
            )

        first_plate = local.create_resource("plate-1", "Parity plate")
        second_plate = local.create_resource("plate-2", "Parity plate")
        for plate, rating in ((first_plate, 12), (second_plate, 3)):
            with local.build_resource(resource_id=plate.id) as builder:
                model = builder.get_model()
                model.properties.metrics.rating = rating
                builder.set_model(model)
        uow = local.backend.begin()
        local.backend.session.execute(
            update(Resource).values(status=LifecycleStatus.ACTIVE)
        )
        uow.commit()

        with local.build_process_template("Parity workflow", "1.0") as template:
            template.add_resource_slot("plate", "container", Direction.input)
            (
                template.add_step("Collect")
                .add_parameters(
                    {"exposure": [{"name": "dwell", "type": "int", "default": 1}]}
                )
                .bind_slot("source", "plate")
                .close_step()
            )

        for name, plate, dwell in (
            ("run-high", first_plate, 15),
            ("run-low", second_plate, 5),
        ):
            with local.build_process_run(
                name, "GraphQL parity run", "Parity workflow", "1.0"
            ) as run:
                run.assign_resource("plate", plate)
                parameters = run.get_params("Collect")
                parameters.exposure.dwell = dwell
                run.set_params(parameters)

        uow = local.backend.begin()
        for model in (ProcessTemplate, ProcessRun, ResourceTemplate, Resource):
            local.backend.session.execute(
                update(model).values(status=LifecycleStatus.ACTIVE)
            )
        uow.commit()

        api_key = "parity-secret"
        app_client = stack.enter_context(
            TestClient(create_app(db_path, api_key=api_key))
        )

        def post(_client, url, *, json, **kwargs):
            assert url.endswith("/graphql")
            return app_client.post("/graphql", json=json, **kwargs)

        monkeypatch.setattr(httpx2.Client, "post", post)
        remote = stack.enter_context(
            RecapClient.from_url("http://recap.test", api_key=api_key)
        )
        yield local, remote


@pytest.mark.parametrize(
    "query",
    [
        pytest.param(lambda q: q.resources().all(), id="default"),
        pytest.param(lambda q: q.resources().include_template().all(), id="template"),
        pytest.param(
            lambda q: q.resources().include("properties").all(), id="properties"
        ),
        pytest.param(lambda q: q.resources().include("children").all(), id="children"),
        pytest.param(lambda q: q.resources(load="eager").all(), id="eager"),
    ],
)
def test_resource_loading_parity(parity_clients, query):
    local, remote = _assert_query_parity(parity_clients, query)
    assert [item.id for item in remote] == [item.id for item in local]


@pytest.mark.parametrize(
    "query",
    [
        pytest.param(lambda q: q.process_runs().all(), id="default"),
        pytest.param(lambda q: q.process_runs().include("steps").all(), id="steps"),
        pytest.param(
            lambda q: q.process_runs().include("steps.parameters").all(),
            id="step-parameters",
        ),
        pytest.param(
            lambda q: q.process_runs().include("resources").all(), id="resources"
        ),
        pytest.param(lambda q: q.process_runs(load="eager").all(), id="eager"),
    ],
)
def test_process_run_loading_parity(parity_clients, query):
    local, remote = _assert_query_parity(parity_clients, query)
    assert [item.id for item in remote] == [item.id for item in local]


def test_namespace_and_template_loading_parity(parity_clients):
    queries = [
        lambda q: q.namespaces().all(),
        lambda q: q.resource_templates().all(),
        lambda q: (
            q.resource_templates()
            .include(["children", "attribute_group_templates", "types"])
            .all()
        ),
        lambda q: q.resource_templates(load="eager").all(),
        lambda q: q.process_templates().all(),
        lambda q: (
            q.process_templates().include(["step_templates", "resource_slots"]).all()
        ),
        lambda q: q.process_templates(load="eager").all(),
    ]
    for query in queries:
        _assert_query_parity(parity_clients, query)


def test_current_actor_permissions_are_typed(parity_clients):
    from recap.authorization.scopes import Scope
    from recap.client.permissions import ActorPermissions

    local, remote = parity_clients
    permissions = remote.permissions()

    assert isinstance(permissions, ActorPermissions)
    assert Scope.RESOURCE_READ in permissions.effective_scopes
    assert permissions.identities[0].subject == "single-user"


@pytest.mark.parametrize(
    ("query", "expected_type"),
    [
        (lambda q: q.resources(shape="ref").all(), ResourceRef),
        (lambda q: q.resource_templates(shape="ref").all(), ResourceTemplateRef),
        (lambda q: q.process_runs(shape="ref").all(), ProcessRunRef),
        (lambda q: q.process_templates(shape="ref").all(), ProcessTemplateRef),
    ],
)
def test_reference_shape_parity(parity_clients, query, expected_type):
    local, remote = _assert_query_parity(parity_clients, query)
    assert all(isinstance(item, expected_type) for item in local)
    assert all(isinstance(item, expected_type) for item in remote)


def test_filters_scopes_pagination_and_count_parity(parity_clients):
    local, remote = parity_clients
    parent = (
        local.namespace(local.namespace_context.path)
        .query_maker()
        .resources()
        .filter(name="plate-1")
        .include("children")
        .first()
    )
    queries = [
        lambda q: q.resources().filter(name="plate-1").all(),
        lambda q: q.resources().filter_property("rating", gt=10, group="metrics").all(),
        lambda q: q.resources().under_parent(parent).all(),
        lambda q: q.resources().descendants(parent).all(),
        lambda q: q.resources().limit(2).all(),
        lambda q: q.resources().offset(1).limit(2).all(),
        lambda q: q.process_runs().filter(name="run-high").all(),
        lambda q: (
            q.process_runs()
            .filter_parameter("dwell", gt=10, group="exposure", step="Collect")
            .all()
        ),
        lambda q: q.process_runs().limit(1).all(),
        lambda q: q.process_runs().offset(1).limit(1).all(),
    ]
    for query in queries:
        _assert_query_parity(parity_clients, query)

    local_q = local.namespace(local.namespace_context.path).query_maker()
    remote_q = remote.namespace(local.namespace_context.path).query_maker()
    count_queries = [
        lambda q: q.resources().count(),
        lambda q: q.resources().filter_property("rating", gt=10).count(),
        lambda q: q.resources().under_parent(parent).count(),
        lambda q: q.process_runs().count(),
        lambda q: q.process_runs().filter_parameter("dwell", gt=10).count(),
    ]
    for query in count_queries:
        assert query(remote_q) == query(local_q)


@pytest.mark.parametrize(
    "query",
    [
        pytest.param(
            lambda q: q.process_runs().where(Field("name").starts_with("run-")).all(),
            id="starts-with",
        ),
        pytest.param(
            lambda q: q.process_runs()
            .where(Field("namespace.path") == "test/mx-parity")
            .all(),
            id="relationship-equality",
        ),
        pytest.param(
            lambda q: q.process_runs().where(Field("name") == "run-high").all(),
            id="equal",
        ),
        pytest.param(
            lambda q: q.process_runs().where(Field("name") != "run-high").all(),
            id="not-equal",
        ),
        pytest.param(
            lambda q: q.process_runs().where(Field("name") > "run-high").all(),
            id="greater-than",
        ),
        pytest.param(
            lambda q: q.process_runs().where(Field("name") >= "run-high").all(),
            id="greater-than-or-equal",
        ),
        pytest.param(
            lambda q: q.process_runs().where(Field("name") < "run-low").all(),
            id="less-than",
        ),
        pytest.param(
            lambda q: q.process_runs().where(Field("name") <= "run-high").all(),
            id="less-than-or-equal",
        ),
        pytest.param(
            lambda q: q.process_runs()
            .where(Field("name").in_(["run-high", "run-low"]))
            .all(),
            id="in",
        ),
        pytest.param(
            lambda q: q.process_runs().where(Field("name").not_in(["run-high"])).all(),
            id="not-in",
        ),
        pytest.param(
            lambda q: q.process_runs().where(Field("name").ends_with("high")).all(),
            id="ends-with",
        ),
        pytest.param(
            lambda q: q.process_runs()
            .where(Field("name").starts_with("run-"))
            .where(Field("name").contains("low"))
            .all(),
            id="chained",
        ),
    ],
)
def test_field_predicate_parity(parity_clients, query):
    _assert_query_parity(parity_clients, query)


@pytest.mark.parametrize(("value", "expected"), [("plate", 0), ("run", 2)])
def test_field_predicate_count_parity(parity_clients, value, expected):
    local, remote = _assert_query_parity(
        parity_clients,
        lambda q: q.process_runs().where(Field("name").contains(value)).count(),
    )
    assert remote == local == expected


@pytest.mark.parametrize("ordering", [Field("name").asc(), Field("name").desc()])
def test_field_ordering_parity(parity_clients, ordering):
    local, remote = _assert_query_parity(
        parity_clients,
        lambda q: q.process_runs().order_by(ordering).all(),
    )
    assert [item.id for item in remote] == [item.id for item in local]


@pytest.mark.parametrize("field", ["id", "create_date"])
def test_transport_scalar_predicate_coercion_parity(parity_clients, field):
    local, _ = parity_clients
    target = (
        local.namespace(local.namespace_context.path)
        .query_maker()
        .process_runs()
        .first()
    )

    local_result, remote_result = _assert_query_parity(
        parity_clients,
        lambda q: q.process_runs().where(Field(field) == getattr(target, field)).all(),
    )

    assert [item.id for item in remote_result] == [item.id for item in local_result]


def _access_outcome(model, field, policy):
    expected = {
        "silent": nullcontext(),
        "warn": pytest.warns(UnloadedFieldWarning),
        "raise": pytest.raises(UnloadedFieldError),
    }[policy]
    with expected as caught:
        value = getattr(model, field)
    if policy == "raise":
        return type(caught.value), str(caught.value)
    if policy == "warn":
        return caught[0].category, str(caught[0].message), _public_dump(value)
    return _public_dump(value)


@pytest.mark.parametrize("policy", ["silent", "warn", "raise"])
@pytest.mark.parametrize(
    ("query", "field"),
    [
        (lambda q, policy: q.resources(on_unloaded=policy), "properties"),
        (lambda q, policy: q.resources(on_unloaded=policy), "children"),
        (lambda q, policy: q.process_runs(on_unloaded=policy), "steps"),
        (
            lambda q, policy: q.process_runs(on_unloaded=policy),
            "assigned_resources",
        ),
    ],
)
def test_on_unloaded_access_behavior_parity(parity_clients, policy, query, field):
    outcomes = []
    for client in parity_clients:
        model = query(
            client.namespace(parity_clients[0].namespace_context.path).query_maker(),
            policy,
        ).first()
        outcomes.append(_access_outcome(model, field, policy))
    assert outcomes[1] == outcomes[0]
