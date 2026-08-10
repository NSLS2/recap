from uuid import UUID

from fastapi.testclient import TestClient

from recap.server.app import create_app


def test_process_run_create_update_finalize_and_replay(tmp_path):
    with TestClient(create_app(tmp_path / "runs.db", api_key="secret")) as client:
        auth = {"Authorization": "Apikey secret"}
        parent = client.put(
            "/api/v1/namespaces/beamline",
            headers={**auth, "Idempotency-Key": "parent"},
            json={},
        )
        assert parent.status_code == 201
        namespace = client.put(
            "/api/v1/namespaces/beamline/amx",
            headers={**auth, "Idempotency-Key": "ns"},
            json={},
        )
        assert namespace.status_code == 201
        template = client.post(
            "/api/v1/process-templates/beamline/amx",
            headers={**auth, "Idempotency-Key": "pt"},
            json={
                "name": "screen",
                "version": "1",
                "resource_slots": [],
                "steps": [
                    {
                        "name": "collect",
                        "parameter_groups": [
                            {
                                "name": "settings",
                                "attributes": [
                                    {
                                        "name": "exposure",
                                        "type": "float",
                                        "default": 0.1,
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
        )
        assert template.status_code == 201

        body = {
            "name": "run-1",
            "description": "first",
            "template_id": template.json()["id"],
            "steps": {"collect": {"parameters": {"settings": {"exposure": 2.5}}}},
        }
        created = client.post(
            "/api/v1/process-runs/beamline/amx",
            headers={**auth, "Idempotency-Key": "run"},
            json=body,
        )
        assert created.status_code == 201, created.text
        assert UUID(created.json()["id"])
        assert "collect" in created.json()["steps"]
        assert created.headers["ETag"] == '"1"'
        assert (
            client.post(
                "/api/v1/process-runs/beamline/amx",
                headers={**auth, "Idempotency-Key": "run"},
                json=body,
            ).json()
            == created.json()
        )

        updated = client.patch(
            f"/api/v1/process-runs/{created.json()['id']}",
            headers={**auth, "Idempotency-Key": "run-update", "If-Match": '"1"'},
            json={"description": "finished", "status": "ACTIVE"},
        )
        assert updated.status_code == 200
        assert updated.json()["description"] == "finished"
        assert updated.json()["status"] == "ACTIVE"
        assert updated.headers["ETag"] == '"2"'


def test_process_run_create_rolls_back_on_bad_parameter(tmp_path):
    with TestClient(create_app(tmp_path / "rollback.db", api_key="secret")) as client:
        auth = {"Authorization": "Apikey secret"}
        client.put(
            "/api/v1/namespaces/n", headers={**auth, "Idempotency-Key": "ns"}, json={}
        )
        template = client.post(
            "/api/v1/process-templates/n",
            headers={**auth, "Idempotency-Key": "pt"},
            json={"name": "pt", "version": "1", "resource_slots": [], "steps": []},
        )
        response = client.post(
            "/api/v1/process-runs/n",
            headers={**auth, "Idempotency-Key": "run"},
            json={
                "name": "bad",
                "description": "bad",
                "template_id": template.json()["id"],
                "steps": {"missing": {"parameters": {}}},
            },
        )
        assert response.status_code == 422
        assert (
            client.post(
                "/api/v1/process-runs/n",
                headers={**auth, "Idempotency-Key": "run-2"},
                json={
                    "name": "bad",
                    "description": "bad",
                    "template_id": template.json()["id"],
                    "steps": {},
                },
            ).status_code
            == 201
        )
