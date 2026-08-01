import inspect

import pytest
from pydantic import ValidationError

from recap.authentication.models import (
    ActorKind,
    IdentityEvidence,
    ProviderIdentity,
    RequestActor,
)
from recap.authentication.protocols import (
    ExternalAuthenticator,
    InternalAuthenticator,
    RequestAuthenticator,
)
from recap.authorization.scopes import DEFAULT_SCOPE_REGISTRY, Scope


def test_identity_requires_provider_and_subject():
    assert ProviderIdentity(provider="pam", subject="alice") != ProviderIdentity(
        provider="oidc", subject="alice"
    )


def test_authentication_models_are_immutable():
    identity = ProviderIdentity(provider="pam", subject="alice")
    evidence = IdentityEvidence(identity=identity)
    actor = RequestActor(
        actor_id="alice",
        kind=ActorKind.USER,
        identities=(identity,),
        credential_scopes=frozenset({Scope.RESOURCE_READ}),
        namespace_restrictions=None,
        credential_fingerprint="abc",
    )

    with pytest.raises(ValidationError):
        identity.subject = "bob"
    with pytest.raises(ValidationError):
        evidence.identity = ProviderIdentity(provider="pam", subject="bob")
    with pytest.raises(ValidationError):
        actor.actor_id = "bob"


def test_scope_registry_is_exact():
    assert set(DEFAULT_SCOPE_REGISTRY.scopes) == {
        Scope.NAMESPACE_READ,
        Scope.NAMESPACE_WRITE,
        Scope.PROCESS_TEMPLATE_READ,
        Scope.PROCESS_TEMPLATE_WRITE,
        Scope.RESOURCE_TEMPLATE_READ,
        Scope.RESOURCE_TEMPLATE_WRITE,
        Scope.RESOURCE_READ,
        Scope.RESOURCE_WRITE,
        Scope.PROCESS_RUN_READ,
        Scope.PROCESS_RUN_WRITE,
    }


def test_write_scope_does_not_imply_read_scope():
    assert not DEFAULT_SCOPE_REGISTRY.allows(
        frozenset({Scope.RESOURCE_WRITE}), Scope.RESOURCE_READ
    )


def test_request_actor_preserves_credential_scope():
    actor = RequestActor(
        actor_id="alice",
        kind=ActorKind.USER,
        identities=(ProviderIdentity(provider="pam", subject="alice"),),
        credential_scopes=frozenset({Scope.RESOURCE_READ}),
        namespace_restrictions=None,
        credential_fingerprint="abc",
    )
    assert actor.credential_scopes == frozenset({Scope.RESOURCE_READ})


def test_authenticator_protocols_are_async():
    assert inspect.iscoroutinefunction(InternalAuthenticator.authenticate)
    assert inspect.iscoroutinefunction(ExternalAuthenticator.authenticate)
    assert inspect.iscoroutinefunction(RequestAuthenticator.authenticate)
