import asyncio
import hashlib
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from recap.authentication.errors import InvalidCredentialsError
from recap.authorization.scopes import Scope


@pytest.fixture
def configured_key():
    return "correct-horse-battery-staple"


@pytest.fixture
def test_client(tmp_path, configured_key):
    from recap.server.app import create_app

    return TestClient(create_app(tmp_path / "test.db", api_key=configured_key))


def test_valid_api_key_authenticates(test_client, configured_key):
    response = test_client.get(
        "/api/v1/namespaces/children",
        headers={"Authorization": f"Apikey {configured_key}"},
    )

    assert response.status_code == 200


def test_api_key_authentication_applies_to_rest_routes(test_client):
    response = test_client.put("/api/v1/namespaces/test", json={})

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Apikey"


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "Apikey wrong",
        "apikey correct-horse-battery-staple",
        "Bearer correct-horse-battery-staple",
        "Apikey",
        "Apikey correct-horse-battery-staple extra",
        "Apikey  correct-horse-battery-staple",
    ],
)
def test_invalid_authorization_header_is_401(test_client, authorization):
    headers = {} if authorization is None else {"Authorization": authorization}

    response = test_client.get("/api/v1/namespaces/children", headers=headers)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Apikey"
    assert "correct-horse-battery-staple" not in response.text


def test_authenticator_returns_deterministic_actor_with_all_scopes(configured_key):
    from recap.authentication.api_key import ApiKeyRequestAuthenticator

    authenticator = ApiKeyRequestAuthenticator(configured_key)
    first = asyncio.run(authenticator.authenticate(configured_key))
    second = asyncio.run(authenticator.authenticate(configured_key))

    assert first == second
    assert first.actor_id == "single-user"
    assert first.credential_scopes == frozenset(Scope)
    assert first.namespace_restrictions is None
    assert (
        first.credential_fingerprint
        == hashlib.sha256(configured_key.encode()).hexdigest()
    )


def test_single_user_actor_identity_is_shared_with_local_actor(configured_key):
    from recap.authentication.actors import single_user_actor
    from recap.authentication.api_key import ApiKeyRequestAuthenticator

    api_key_actor = asyncio.run(
        ApiKeyRequestAuthenticator(configured_key).authenticate(configured_key)
    )
    local_actor = single_user_actor(credential_fingerprint="local-single-user")

    assert api_key_actor.actor_id == local_actor.actor_id
    assert api_key_actor.kind == local_actor.kind
    assert api_key_actor.identities[0].subject == local_actor.identities[0].subject
    assert api_key_actor.identities[0].provider == "api-key"
    assert local_actor.identities[0].provider == "single-user"
    assert api_key_actor.credential_scopes == local_actor.credential_scopes
    assert api_key_actor.namespace_restrictions == local_actor.namespace_restrictions
    assert api_key_actor.credential_fingerprint != local_actor.credential_fingerprint


def test_authenticator_uses_constant_time_comparison(configured_key):
    from recap.authentication.api_key import ApiKeyRequestAuthenticator

    authenticator = ApiKeyRequestAuthenticator(configured_key)
    with patch("recap.authentication.api_key.secrets.compare_digest") as compare:
        compare.return_value = True
        asyncio.run(authenticator.authenticate("presented-key"))

    compare.assert_called_once_with("presented-key", configured_key)


def test_authenticator_errors_and_repr_redact_secret(configured_key):
    from recap.authentication.api_key import ApiKeyRequestAuthenticator

    authenticator = ApiKeyRequestAuthenticator(configured_key)

    assert configured_key not in repr(authenticator)
    with pytest.raises(InvalidCredentialsError) as exc_info:
        asyncio.run(authenticator.authenticate(f"wrong-{configured_key}"))
    assert configured_key not in str(exc_info.value)
