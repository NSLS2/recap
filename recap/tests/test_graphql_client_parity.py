from contextlib import nullcontext

import pytest

from recap.dsl.query import Field, QuerySpec
from recap.exceptions import UnloadedFieldError, UnloadedFieldWarning
from recap.schemas.process import ProcessRunRef, ProcessTemplateRef
from recap.schemas.resource import ResourceRef, ResourceSchema, ResourceTemplateRef


def _public_dump(value):
    if isinstance(value, list | tuple):
        return [_public_dump(item) for item in value]
    if isinstance(value, dict):
        return {key: _public_dump(item) for key, item in value.items()}
    if not hasattr(value, "model_dump"):
        return value
    return value.model_dump(mode="json", by_alias=True)


def _assert_query_parity(clients, query, namespace_path=None):
    local, remote = clients
    namespace_path = namespace_path or local.namespace_context.path
    local_result = query(local.namespace(namespace_path).query_maker())
    remote_result = query(remote.namespace(namespace_path).query_maker())
    assert _public_dump(remote_result) == _public_dump(local_result)
    return local_result, remote_result


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


def test_namespace_loading_parity(parity_clients):
    local, remote = _assert_query_parity(
        parity_clients,
        lambda q: q.namespaces().all(),
        namespace_path="test/mx-parity",
    )
    expected_paths = []
    assert [item.path for item in local] == expected_paths
    assert [item.path for item in remote] == expected_paths
    scoped_clients = [client.namespace("test/mx-parity") for client in parity_clients]
    try:
        for client in scoped_clients:
            assert client.namespace_path == "test/mx-parity"
    finally:
        for client in scoped_clients:
            client.close()


@pytest.mark.parametrize(
    "query",
    [
        lambda q: q.resource_templates().all(),
        lambda q: q.resource_templates()
        .include(["children", "attribute_group_templates", "types"])
        .all(),
        lambda q: q.resource_templates(load="eager").all(),
    ],
)
def test_resource_template_loading_parity(parity_clients, query):
    local, remote = _assert_query_parity(
        parity_clients, query, namespace_path="test/mx-parity"
    )
    expected = [("Parity plate", "1.0")]
    assert [(item.name, item.version) for item in local] == expected
    assert [(item.name, item.version) for item in remote] == expected


@pytest.mark.parametrize(
    "query",
    [
        lambda q: q.process_templates().all(),
        lambda q: q.process_templates()
        .include(["step_templates", "resource_slots"])
        .all(),
        lambda q: q.process_templates(load="eager").all(),
    ],
)
def test_process_template_loading_parity(parity_clients, query):
    local, remote = _assert_query_parity(
        parity_clients, query, namespace_path="test/mx-parity"
    )
    expected = [("Parity workflow", "1.0")]
    assert [(item.name, item.version) for item in local] == expected
    assert [(item.name, item.version) for item in remote] == expected


def test_current_actor_permissions_are_typed(parity_clients):
    from recap.authorization.scopes import Scope
    from recap.client.permissions import ActorPermissions

    local, remote = parity_clients
    permissions = remote.permissions()

    assert isinstance(permissions, ActorPermissions)
    assert Scope.RESOURCE_READ in permissions.effective_scopes
    assert permissions.identities[0].provider == "api-key"
    assert permissions.identities[0].subject == "single-user"


def test_mutable_resource_visibility_parity(parity_clients):
    local, remote = parity_clients
    hidden = local.create_resource("mutable-only", "Parity plate")
    namespace_path = local.namespace_context.path

    default_local = local._read_backend.query(
        ResourceSchema,
        QuerySpec(filters={"name": hidden.name}),
        namespace_path=namespace_path,
    )
    default_remote = remote._read_backend.query(
        ResourceSchema,
        QuerySpec(filters={"name": hidden.name}),
        namespace_path=namespace_path,
    )
    assert default_local == default_remote == []


def test_parity_clients_use_isolated_seed_copy(parity_clients):
    local, _ = parity_clients
    local.create_resource("fixture-isolation", "Parity plate")
    assert local.get_resource("fixture-isolation", "Parity plate").name == "fixture-isolation"


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
