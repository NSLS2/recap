from unittest.mock import Mock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from recap.adapter.local import LocalBackend
from recap.authentication.models import ActorKind, ProviderIdentity, RequestActor
from recap.authorization.policy import SnapshotNamespacePolicy
from recap.authorization.query import AuthorizedQuery
from recap.authorization.scopes import Scope
from recap.authorization.snapshot import (
    AuthorizationSnapshot,
    GrantProvenance,
    SnapshotMetadata,
)
from recap.db.base import Base
from recap.db.namespace import Namespace
from recap.db.process import ProcessRun, ProcessTemplate
from recap.dsl.query import FieldOrdering, QuerySpec
from recap.lifecycle import LifecycleStatus
from recap.schemas.namespace import NamespaceSchema
from recap.schemas.process import ProcessRunSchema
from recap.server.app import create_app

IDENTITY = ProviderIdentity(provider="api-key", subject="single-user")


def _actor() -> RequestActor:
    return RequestActor(
        actor_id="single-user",
        kind=ActorKind.USER,
        identities=(IDENTITY,),
        credential_scopes=frozenset(Scope),
        namespace_restrictions=None,
        credential_fingerprint="fingerprint",
    )


def _grant(path: str, scope: Scope, *, group: str = "scientists", role: str = "member"):
    return GrantProvenance(
        identity=IDENTITY,
        namespace_path=path,
        scope=scope,
        group=group,
        role=role,
    )


def _snapshot(*grants: GrantProvenance) -> AuthorizationSnapshot:
    return AuthorizationSnapshot(
        metadata=SnapshotMetadata(format_version=1, source_revision="generation-7"),
        grants=frozenset(grants),
    )


def _backend(tmp_path, namespaces: list[tuple[str, LifecycleStatus]]) -> LocalBackend:
    engine = create_engine(f"sqlite:///{tmp_path / 'authorized.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory.begin() as session:
        session.add_all(
            Namespace(path=path, status=status, metadata_json={})
            for path, status in namespaces
        )
    return LocalBackend(factory)


def test_authorized_query_enforces_visibility_before_count_and_pagination(tmp_path):
    backend = _backend(
        tmp_path,
        [
            ("beamline", LifecycleStatus.ACTIVE),
            ("beamline/amx", LifecycleStatus.ACTIVE),
            ("beamline/amx/run", LifecycleStatus.MUTABLE),
            ("beamline/fmx", LifecycleStatus.ACTIVE),
            ("beamline/amx/run/archived", LifecycleStatus.ARCHIVED),
        ],
    )
    policy = SnapshotNamespacePolicy(
        _snapshot(
            _grant("beamline/amx", Scope.NAMESPACE_READ),
            _grant("beamline/amx/run", Scope.NAMESPACE_WRITE),
        )
    )
    authorization = AuthorizedQuery.from_policy(
        policy, _actor(), namespace_path="beamline/amx/run"
    )
    ordered = QuerySpec(orderings=[FieldOrdering(field="path")])

    visible = backend.query_authorized(
        NamespaceSchema, ordered, authorization=authorization
    )
    page = backend.query_authorized(
        NamespaceSchema,
        QuerySpec(
            orderings=[FieldOrdering(field="path")],
            offset=1,
            limit=1,
            include_archived=True,
        ),
        authorization=authorization,
    )

    assert [item.path for item in visible] == [
        "beamline",
        "beamline/amx",
        "beamline/amx/run",
    ]
    assert [item.path for item in page] == ["beamline/amx"]
    assert (
        backend.count_authorized(
            NamespaceSchema,
            QuerySpec(include_archived=True),
            authorization=authorization,
        )
        == 3
    )


def test_graphql_requires_configured_api_key(tmp_path):
    response = TestClient(create_app(tmp_path / "auth.db", api_key="secret")).post(
        "/graphql", json={"query": "{ __typename }"}
    )

    assert response.status_code == 401


def test_process_runs_are_visible_only_in_exact_context(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'runs.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory.begin() as session:
        parent = Namespace(
            path="beamline/amx", status=LifecycleStatus.ACTIVE, metadata_json={}
        )
        context = Namespace(
            path="beamline/amx/run",
            parent=parent,
            status=LifecycleStatus.ACTIVE,
            metadata_json={},
        )
        parent_template = ProcessTemplate(
            namespace=parent,
            name="parent-template",
            version="1",
            status=LifecycleStatus.ACTIVE,
        )
        context_template = ProcessTemplate(
            namespace=context,
            name="context-template",
            version="1",
            status=LifecycleStatus.ACTIVE,
        )
        session.add_all(
            [
                ProcessRun(
                    namespace=parent,
                    name="parent-run",
                    description="ancestor must remain hidden",
                    template=parent_template,
                    status=LifecycleStatus.ACTIVE,
                ),
                ProcessRun(
                    namespace=context,
                    name="context-run",
                    description="exact context",
                    template=context_template,
                    status=LifecycleStatus.ACTIVE,
                ),
            ]
        )

    authorization = AuthorizedQuery.from_policy(
        SnapshotNamespacePolicy(
            _snapshot(_grant("beamline/amx/run", Scope.PROCESS_RUN_READ))
        ),
        _actor(),
        namespace_path="beamline/amx/run",
    )
    runs = LocalBackend(factory).query_authorized(
        ProcessRunSchema, QuerySpec(), authorization=authorization
    )

    assert [run.name for run in runs] == ["context-run"]


def test_graphql_fixes_one_snapshot_and_reports_only_current_actor(tmp_path):
    provider = Mock()
    provider.acquire.return_value = _snapshot(
        _grant("beamline/amx", Scope.RESOURCE_READ, group="amx-users", role="reader"),
        GrantProvenance(
            identity=ProviderIdentity(provider="oidc", subject="someone-else"),
            namespace_path="beamline/amx",
            scope=Scope.RESOURCE_WRITE,
            group="admins",
            role="owner",
        ),
    )
    app = create_app(tmp_path / "permissions.db", api_key="secret")
    app.state.authorization_snapshot_provider = provider
    response = TestClient(app).post(
        "/graphql",
        headers={"Authorization": "Apikey secret"},
        json={
            "query": """
                {
                  first: permissions(namespace_path: "beamline/amx") {
                    snapshot_generation
                    identities { provider subject }
                    effective_scopes
                    matched_namespace_paths
                    groups
                    roles
                  }
                  second: permissions(namespace_path: "beamline/amx") {
                    snapshot_generation
                  }
                }
            """
        },
    )

    assert response.status_code == 200
    assert response.json().get("errors") is None
    assert response.json()["data"]["first"] == {
        "snapshot_generation": "generation-7",
        "identities": [{"provider": "api-key", "subject": "single-user"}],
        "effective_scopes": ["resource:read"],
        "matched_namespace_paths": ["beamline/amx"],
        "groups": ["amx-users"],
        "roles": ["reader"],
    }
    assert provider.acquire.call_count == 1
