from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from recap.authorization.snapshot import SnapshotUnavailable
from recap.server.errors import AuthorizationDenied


def _assert_request_id(response):
    header_id = response.headers["X-Request-ID"]
    parsed = UUID(header_id)
    assert parsed.version == 4
    assert response.json()["error"]["request_id"] == header_id


def test_success_has_server_generated_uuid4_that_cannot_be_spoofed(tmp_path):
    from recap.server.app import create_app

    client = TestClient(create_app(tmp_path / "test.db"))
    response = client.get("/db_path", headers={"X-Request-ID": "caller-id"})

    assert response.status_code == 200
    request_id = response.headers["X-Request-ID"]
    assert UUID(request_id).version == 4
    assert request_id != "caller-id"


def test_authentication_failure_uses_safe_stable_envelope(tmp_path):
    from recap.server.app import create_app

    secret = "correct-horse-battery-staple"
    client = TestClient(create_app(tmp_path / "test.db", api_key=secret))
    response = client.get(
        "/db_path",
        headers={"Authorization": f"Apikey wrong-{secret}", "X-Request-ID": "spoof"},
    )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Apikey"
    assert response.json()["error"]["code"] == "authentication_required"
    assert secret not in response.text
    _assert_request_id(response)


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (AuthorizationDenied(), 403, "permission_denied"),
        (AuthorizationDenied(conceal=True), 404, "not_found"),
        (SnapshotUnavailable("grant details: top-secret"), 503, "service_unavailable"),
    ],
)
def test_security_failures_map_to_safe_envelopes(
    tmp_path, error, status_code, code
):
    from recap.server.app import create_app

    app = create_app(tmp_path / "test.db")

    @app.get("/failure")
    def failure():
        raise error

    response = TestClient(app).get("/failure")

    assert response.status_code == status_code
    assert response.json()["error"]["code"] == code
    assert "top-secret" not in response.text
    assert "grant" not in response.text.lower()
    _assert_request_id(response)


def test_validation_failure_is_sanitized_422(tmp_path):
    from recap.server.app import create_app

    app = create_app(tmp_path / "test.db")

    @app.get("/validated")
    def validated(secret_parameter: int):
        return {"value": secret_parameter}

    response = TestClient(app).get(
        "/validated", params={"secret_parameter": "sensitive-value"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert "secret_parameter" not in response.text
    assert "sensitive-value" not in response.text
    _assert_request_id(response)


def test_http_and_internal_failures_never_expose_details(tmp_path):
    from recap.server.app import create_app

    app = create_app(tmp_path / "test.db")

    @app.get("/missing")
    def missing():
        raise HTTPException(status_code=404, detail="private object identifier")

    @app.get("/internal")
    def internal():
        raise RuntimeError("credential=server-secret")

    with TestClient(app, raise_server_exceptions=False) as client:
        missing_response = client.get("/missing")
        internal_response = client.get("/internal")

    assert missing_response.status_code == 404
    assert missing_response.json()["error"]["code"] == "not_found"
    assert "private object identifier" not in missing_response.text
    _assert_request_id(missing_response)

    assert internal_response.status_code == 500
    assert internal_response.json()["error"]["code"] == "internal_error"
    assert "server-secret" not in internal_response.text
    _assert_request_id(internal_response)
