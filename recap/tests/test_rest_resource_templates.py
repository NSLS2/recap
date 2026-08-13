def draft(name="plate", version="1.0"):
    return {
        "name": name,
        "version": version,
        "type_names": ["container", "plate"],
        "property_groups": [
            {
                "name": "dimensions",
                "attributes": [{"name": "rows", "type": "int", "default": 8}],
            }
        ],
        "children": [{"name": "well", "version": "1.0", "type_names": ["well"]}],
    }

def test_post_and_patch_resource_template_aggregate(
    api_client, idempotency_headers, create_namespace
):
    create_namespace()
    created = api_client.post(
        "/api/v1/resource-templates/beamline/amx",
        headers=idempotency_headers("template-1"),
        json=draft(),
    )
    assert created.status_code == 201
    assert created.json()["children"]["well"]["types"][0]["name"] == "well"
    assert created.headers["ETag"] == '"1"'

    updated = api_client.patch(
        f"/api/v1/resource-templates/{created.json()['id']}",
        headers=idempotency_headers("template-2", **{"If-Match": created.headers["ETag"]}),
        json=draft("plate-updated"),
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "plate-updated"
    assert updated.headers["ETag"] == '"2"'


def test_resource_template_route_replays_and_has_no_granular_child_route(
    api_client, idempotency_headers, create_namespace
):
    create_namespace()
    request = {"headers": idempotency_headers("template-1"), "json": draft()}
    first = api_client.post("/api/v1/resource-templates/beamline/amx", **request)
    replay = api_client.post("/api/v1/resource-templates/beamline/amx", **request)
    assert replay.json() == first.json()
    assert (
        api_client.post(
            f"/api/v1/resource-templates/{first.json()['id']}/children",
            headers=idempotency_headers("child-1"),
            json={},
        ).status_code
        == 422
    )
