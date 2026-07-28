import pytest

from recap.authentication.models import ActorKind, ProviderIdentity, RequestActor
from recap.authorization.policy import (
    DenialCode,
    SnapshotNamespacePolicy,
    UnrestrictedNamespacePolicy,
)
from recap.authorization.scopes import Scope
from recap.authorization.snapshot import (
    AuthorizationSnapshot,
    GrantProvenance,
    SnapshotMetadata,
)


def actor(
    *identities: ProviderIdentity,
    scopes: frozenset[Scope] = frozenset(Scope),
    restrictions: frozenset[str] | None = None,
) -> RequestActor:
    return RequestActor(
        actor_id="current-actor",
        kind=ActorKind.USER,
        identities=identities,
        credential_scopes=scopes,
        namespace_restrictions=restrictions,
        credential_fingerprint="sha256:test",
    )


def grant(
    identity: ProviderIdentity,
    namespace_path: str,
    scope: Scope,
    *,
    group: str = "scientists",
    role: str = "member",
) -> GrantProvenance:
    return GrantProvenance(
        identity=identity,
        namespace_path=namespace_path,
        scope=scope,
        group=group,
        role=role,
    )


def policy(*grants: GrantProvenance) -> SnapshotNamespacePolicy:
    return SnapshotNamespacePolicy(
        AuthorizationSnapshot(
            metadata=SnapshotMetadata(format_version=1, source_revision="revision-7"),
            grants=frozenset(grants),
        )
    )


def test_inherits_ancestor_grants_but_isolates_siblings_and_matches_provider_exactly():
    pam_alice = ProviderIdentity(provider="pam", subject="alice")
    oidc_alice = ProviderIdentity(provider="oidc", subject="alice")
    namespace_policy = policy(
        grant(pam_alice, "beamline", Scope.NAMESPACE_READ),
        grant(oidc_alice, "beamline/amx", Scope.RESOURCE_READ),
        grant(pam_alice, "beamline/fmx", Scope.RESOURCE_WRITE),
    )

    permissions = namespace_policy.permissions_for(
        actor(pam_alice), "beamline/amx/proposal/123"
    )

    assert permissions.snapshot_generation == "revision-7"
    assert permissions.identities == (pam_alice,)
    assert permissions.effective_scopes == frozenset({Scope.NAMESPACE_READ})
    assert permissions.matched_namespace_paths == ("beamline",)
    assert permissions.grants == (grant(pam_alice, "beamline", Scope.NAMESPACE_READ),)


def test_unions_identity_grants_then_intersects_credential_scope_and_restriction():
    pam_alice = ProviderIdentity(provider="pam", subject="alice")
    oidc_alice = ProviderIdentity(provider="oidc", subject="alice@example.com")
    namespace_policy = policy(
        grant(pam_alice, "beamline", Scope.NAMESPACE_READ),
        grant(oidc_alice, "beamline/amx", Scope.RESOURCE_READ),
        grant(pam_alice, "beamline/amx", Scope.RESOURCE_WRITE),
    )
    current_actor = actor(
        pam_alice,
        oidc_alice,
        scopes=frozenset({Scope.NAMESPACE_READ, Scope.RESOURCE_READ}),
        restrictions=frozenset({"beamline/amx"}),
    )

    permissions = namespace_policy.permissions_for(current_actor, "beamline/amx/run")

    assert permissions.effective_scopes == frozenset(
        {Scope.NAMESPACE_READ, Scope.RESOURCE_READ}
    )
    assert {item.scope for item in permissions.grants} == {
        Scope.NAMESPACE_READ,
        Scope.RESOURCE_READ,
    }
    assert (
        namespace_policy.permissions_for(current_actor, "beamline/fmx").effective_scopes
        == frozenset()
    )


def test_write_grant_never_implies_read_and_denial_has_safe_code_only():
    identity = ProviderIdentity(provider="pam", subject="alice")
    namespace_policy = policy(grant(identity, "beamline/amx", Scope.RESOURCE_WRITE))

    decision = namespace_policy.authorize(
        actor(identity), Scope.RESOURCE_READ, "beamline/amx"
    )

    assert not decision.allowed
    assert decision.denial_code is DenialCode.INSUFFICIENT_SCOPE
    assert decision.permissions is None
    assert decision.model_dump() == {
        "allowed": False,
        "denial_code": DenialCode.INSUFFICIENT_SCOPE,
        "permissions": None,
    }


def test_process_run_requires_exact_context_but_templates_and_resources_allow_ancestors():
    identity = ProviderIdentity(provider="pam", subject="alice")
    namespace_policy = policy(
        grant(identity, "beamline", Scope.PROCESS_RUN_READ),
        grant(identity, "beamline", Scope.PROCESS_TEMPLATE_READ),
        grant(identity, "beamline", Scope.RESOURCE_READ),
    )
    current_actor = actor(identity)

    assert not namespace_policy.authorize_process_run(
        current_actor,
        Scope.PROCESS_RUN_READ,
        context_namespace_path="beamline/amx",
        target_namespace_path="beamline",
    ).allowed
    assert namespace_policy.authorize_template(
        current_actor,
        Scope.PROCESS_TEMPLATE_READ,
        context_namespace_path="beamline/amx",
        target_namespace_path="beamline",
    ).allowed
    assert namespace_policy.authorize_resource(
        current_actor,
        Scope.RESOURCE_READ,
        context_namespace_path="beamline/amx",
        target_namespace_path="beamline",
    ).allowed


def test_proposal_read_does_not_grant_parent_update():
    identity = ProviderIdentity(provider="pam", subject="alice")
    namespace_policy = policy(
        grant(identity, "beamline/amx/proposal/123", Scope.NAMESPACE_READ),
        grant(identity, "beamline/amx/proposal/123", Scope.NAMESPACE_WRITE),
    )

    assert namespace_policy.authorize(
        actor(identity), Scope.NAMESPACE_READ, "beamline/amx/proposal/123"
    ).allowed
    assert not namespace_policy.authorize(
        actor(identity), Scope.NAMESPACE_WRITE, "beamline/amx"
    ).allowed


def test_namespace_creation_requires_parent_write_authorization():
    identity = ProviderIdentity(provider="pam", subject="alice")
    namespace_policy = policy(grant(identity, "beamline/amx", Scope.NAMESPACE_WRITE))

    assert namespace_policy.authorize_namespace_create(
        actor(identity), "beamline/amx/proposal"
    ).allowed
    denied = namespace_policy.authorize_namespace_create(
        actor(identity), "beamline/fmx/proposal"
    )
    assert not denied.allowed
    assert denied.denial_code is DenialCode.INSUFFICIENT_SCOPE


@pytest.mark.parametrize(
    ("missing_scope", "expected_code"),
    [
        (Scope.RESOURCE_READ, DenialCode.SOURCE_READ_REQUIRED),
        (Scope.RESOURCE_WRITE, DenialCode.DESTINATION_WRITE_REQUIRED),
    ],
)
def test_copy_requires_source_read_and_destination_write(
    missing_scope: Scope, expected_code: DenialCode
):
    identity = ProviderIdentity(provider="pam", subject="alice")
    grants = [
        grant(identity, "source", Scope.RESOURCE_READ),
        grant(identity, "destination", Scope.RESOURCE_WRITE),
    ]
    namespace_policy = policy(
        *(item for item in grants if item.scope is not missing_scope)
    )

    decision = namespace_policy.authorize_copy(
        actor(identity),
        source_namespace_path="source",
        destination_namespace_path="destination",
    )

    assert not decision.allowed
    assert decision.denial_code is expected_code
    assert decision.permissions is None


def test_copy_is_allowed_when_both_sides_are_authorized():
    identity = ProviderIdentity(provider="pam", subject="alice")
    namespace_policy = policy(
        grant(identity, "source", Scope.RESOURCE_READ),
        grant(identity, "destination", Scope.RESOURCE_WRITE),
    )

    assert namespace_policy.authorize_copy(
        actor(identity),
        source_namespace_path="source",
        destination_namespace_path="destination",
    ).allowed


def test_unrestricted_policy_grants_single_user_all_scopes_without_provenance():
    current_actor = actor(ProviderIdentity(provider="api-key", subject="single-user"))
    namespace_policy = UnrestrictedNamespacePolicy()

    permissions = namespace_policy.permissions_for(current_actor, "beamline/amx")

    assert permissions.effective_scopes == frozenset(Scope)
    assert permissions.snapshot_generation is None
    assert permissions.matched_namespace_paths == ()
    assert permissions.grants == ()
    assert namespace_policy.authorize_copy(
        current_actor,
        source_namespace_path="source",
        destination_namespace_path="destination",
    ).allowed
