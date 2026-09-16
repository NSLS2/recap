from uuid import UUID


def draft(labels=None):
    return {
        "name": "screening",
        "version": "1.0",
        "labels": labels or ["mx"],
        "resource_slots": [
            {
                "name": "sample",
                "resource_type": "container",
                "direction": "input",
                "create_resource_type": True,
            }
        ],
        "steps": [
            {
                "name": "collect",
                "role_bindings": {"source": "sample"},
                "parameter_groups": [
                    {
                        "name": "exposure",
                        "attributes": [
                            {
                                "name": "duration",
                                "type": "float",
                                "unit": "s",
                                "default": 0.1,
                            }
                        ],
                    }
                ],
            }
        ],
    }

def test_post_and_patch_process_template_routes(
    api_client, idempotency_headers, create_namespace
):
    create_namespace()
    created = api_client.post(
        "/api/v1/process-templates/beamline/amx",
        headers=idempotency_headers("template-1"),
        json=draft(),
    )

    assert created.status_code == 201
    UUID(created.json()["id"])
    assert (
        created.json()["step_templates"]["collect"]["resource_slots"]["source"]["name"]
        == "sample"
    )
    assert created.headers["ETag"] == '"1"'

    updated = api_client.patch(
        f"/api/v1/process-templates/{created.json()['id']}",
        headers=idempotency_headers("template-2", **{"If-Match": created.headers["ETag"]}),
        json=draft(["updated"]),
    )
    assert updated.status_code == 200
    assert updated.json()["labels"] == ["updated"]
    assert updated.headers["ETag"] == '"2"'


def test_process_template_route_replay_and_validation(
    api_client, idempotency_headers, create_namespace
):
    create_namespace()
    request = {
        "headers": idempotency_headers("template-1"),
        "json": draft(),
    }
    first = api_client.post("/api/v1/process-templates/beamline/amx", **request)
    replay = api_client.post("/api/v1/process-templates/beamline/amx", **request)
    invalid = draft()
    invalid["steps"][0]["role_bindings"] = {"source": "missing"}
    rejected = api_client.post(
        "/api/v1/process-templates/beamline/amx",
        headers=idempotency_headers("template-2"),
        json=invalid,
    )

    assert replay.json() == first.json()
    assert rejected.status_code == 422
