import pytest

from .command_responses import command_data, command_model


def _seed_lifecycle_entity(
    api_client, idempotency_headers, create_namespace, object_type
):
    create_namespace()
    if object_type == "resource_template":
        response = api_client.post(
            "/api/v1/resource-templates/beamline/amx",
            headers=idempotency_headers("template"),
            json={"name": "plate", "version": "1", "type_names": ["plate"]},
        )
    elif object_type == "resource":
        template = api_client.post(
            "/api/v1/resource-templates/beamline/amx",
            headers=idempotency_headers("template"),
            json={"name": "plate", "version": "1", "type_names": ["plate"]},
        )
        response = api_client.post(
            "/api/v1/resources/beamline/amx",
            headers=idempotency_headers("resource"),
            json={"name": "sample", "template_id": command_data(template)["id"]},
        )
    elif object_type == "process_template":
        response = api_client.post(
            "/api/v1/process-templates/beamline/amx",
            headers=idempotency_headers("process-template"),
            json={"name": "scan", "version": "1", "resource_slots": [], "steps": []},
        )
    else:
        template = api_client.post(
            "/api/v1/process-templates/beamline/amx",
            headers=idempotency_headers("process-template"),
            json={"name": "scan", "version": "1", "resource_slots": [], "steps": []},
        )
        response = api_client.post(
            "/api/v1/process-runs/beamline/amx",
            headers=idempotency_headers("process-run"),
            json={
                "name": "run",
                "description": "queued",
                "template_id": command_data(template)["id"],
                "steps": {},
            },
        )
    assert response.status_code == 201, response.text
    return response


@pytest.mark.parametrize(
    "object_type", ["resource", "resource_template", "process_template", "process_run"]
)
def test_lifecycle_route_dispatches_supported_object_types(
    api_client, idempotency_headers, create_namespace, object_type
):
    entity = _seed_lifecycle_entity(
        api_client, idempotency_headers, create_namespace, object_type
    )

    updated = api_client.post(
        f"/api/v1/lifecycle/{object_type}/{command_data(entity)['id']}",
        headers=idempotency_headers("lifecycle", **{"If-Match": '"1"'}),
        json={"status": "ACTIVE"},
    )

    assert updated.status_code == 200, updated.text
    assert command_data(updated)["status"] == "ACTIVE"
    assert updated.headers["ETag"] == '"2"'

    replayed = api_client.post(
        f"/api/v1/lifecycle/{object_type}/{command_data(entity)['id']}",
        headers=idempotency_headers("lifecycle", **{"If-Match": '"1"'}),
        json={"status": "ACTIVE"},
    )
    assert replayed.status_code == 200, replayed.text
    for field in ("id", "name", "description", "version", "status", "revision"):
        if field in command_data(updated):
            assert command_data(replayed)[field] == command_data(updated)[field]
    assert command_data(replayed)["revision"] == command_data(updated)["revision"] == 2
    assert replayed.headers["ETag"] == updated.headers["ETag"] == '"2"'

    archived = api_client.post(
        f"/api/v1/lifecycle/{object_type}/{command_data(entity)['id']}",
        headers=idempotency_headers("lifecycle-archive", **{"If-Match": '"2"'}),
        json={"status": "ARCHIVED"},
    )
    assert archived.status_code == 200, archived.text
    assert command_data(archived)["status"] == "ARCHIVED"
    assert command_data(archived)["revision"] == 3
    assert archived.headers["ETag"] == '"3"'


def _seed_resource_tree(api_client, idempotency_headers, create_namespace):
    root_response = _seed_lifecycle_entity(
        api_client, idempotency_headers, create_namespace, "resource"
    )
    root = command_model(root_response)
    nodes = {"root": root}
    for name, parent in [
        ("child", "root"),
        ("grandchild", "child"),
        ("leaf", "grandchild"),
        ("sibling", "root"),
    ]:
        response = api_client.post(
            "/api/v1/resources/beamline/amx",
            headers=idempotency_headers(name),
            json={
                "name": name,
                "template_id": str(root.template.id),
                "parent_id": str(nodes[parent].id),
            },
        )
        assert response.status_code == 201, response.text
        nodes[name] = command_model(response)
    return nodes


def _activate_tree(api_client, idempotency_headers, root, key="finalize", **options):
    return api_client.post(
        f"/api/v1/lifecycle/resource/{root.id}",
        headers=idempotency_headers(key, **{"If-Match": f'"{root.revision}"'}),
        json={"status": "ACTIVE", **options},
    )


@pytest.mark.parametrize(
    ("options", "statuses"),
    [
        ({}, ["ACTIVE", "MUTABLE", "MUTABLE", "MUTABLE"]),
        ({"recursive_depth": 0}, ["ACTIVE", "MUTABLE", "MUTABLE", "MUTABLE"]),
        ({"recursive_depth": 1}, ["ACTIVE", "ACTIVE", "MUTABLE", "MUTABLE"]),
        ({"recursive_depth": 2}, ["ACTIVE", "ACTIVE", "ACTIVE", "MUTABLE"]),
        ({"recursive_depth": None}, ["ACTIVE", "ACTIVE", "ACTIVE", "ACTIVE"]),
    ],
)
def test_resource_finalization_respects_depth(
    api_client, idempotency_headers, create_namespace, options, statuses
):
    nodes = _seed_resource_tree(api_client, idempotency_headers, create_namespace)
    response = _activate_tree(api_client, idempotency_headers, nodes["root"], **options)
    assert response.status_code == 200, response.text
    root = command_model(response)
    chain = [
        root,
        root.children["child"],
        root.children["child"].children["grandchild"],
        root.children["child"].children["grandchild"].children["leaf"],
    ]
    assert [node.status.value for node in chain] == statuses
    assert [node.revision for node in chain] == [
        2 if status == "ACTIVE" else 1 for status in statuses
    ]
    assert root.children["sibling"].status.value == (
        "ACTIVE" if options.get("recursive_depth", 0) != 0 else "MUTABLE"
    )


def test_resource_finalization_prunes_archived_branches(
    api_client, idempotency_headers, create_namespace
):
    nodes = _seed_resource_tree(api_client, idempotency_headers, create_namespace)
    child = nodes["child"]
    archived_response = api_client.post(
        f"/api/v1/lifecycle/resource/{child.id}",
        headers=idempotency_headers("archive-child", **{"If-Match": '"1"'}),
        json={"status": "ARCHIVED"},
    )
    assert archived_response.status_code == 200
    archived = command_model(archived_response)
    response = _activate_tree(
        api_client, idempotency_headers, nodes["root"], recursive_depth=None
    )
    assert response.status_code == 200, response.text
    root = command_model(response)
    assert root.status.value == root.children["sibling"].status.value == "ACTIVE"
    skipped = root.children["child"]
    assert skipped.status.value == "ARCHIVED"
    assert (skipped.revision, skipped.modified_date) == (
        archived.revision,
        archived.modified_date,
    )
    assert skipped.children["grandchild"].status.value == "MUTABLE"
    assert skipped.children["grandchild"].revision == 1
    assert skipped.children["grandchild"].children["leaf"].status.value == "MUTABLE"


@pytest.mark.parametrize("status", ["ACTIVE", "ARCHIVED"])
def test_resource_finalization_keeps_unchanged_parent_metadata(
    api_client, idempotency_headers, create_namespace, status
):
    nodes = _seed_resource_tree(api_client, idempotency_headers, create_namespace)
    transition = api_client.post(
        f"/api/v1/lifecycle/resource/{nodes['root'].id}",
        headers=idempotency_headers("parent-status", **{"If-Match": '"1"'}),
        json={"status": status},
    )
    parent = command_model(transition)
    response = _activate_tree(
        api_client, idempotency_headers, parent, recursive_depth=None
    )
    assert response.status_code == 200, response.text
    root = command_model(response)
    assert (root.status, root.revision, root.modified_date) == (
        parent.status,
        parent.revision,
        parent.modified_date,
    )
    expected = "ACTIVE" if status == "ACTIVE" else "MUTABLE"
    assert root.children["child"].status.value == expected
    assert root.children["child"].children["grandchild"].status.value == expected


def test_resource_finalization_stale_parent_rolls_back_children(
    api_client, idempotency_headers, create_namespace
):
    nodes = _seed_resource_tree(api_client, idempotency_headers, create_namespace)
    response = _activate_tree(
        api_client, idempotency_headers, nodes["root"], key="parent-only"
    )
    parent = command_model(response)
    failed = _activate_tree(
        api_client, idempotency_headers, nodes["root"], recursive_depth=None
    )
    assert failed.status_code == 409, failed.text
    # A no-op request returns the committed graph without changing revisions.
    unchanged = command_model(
        _activate_tree(api_client, idempotency_headers, parent, key="read-parent")
    )
    assert unchanged.children["child"].status.value == "MUTABLE"
    assert unchanged.revision == parent.revision


def test_recursive_resource_finalization_replays_original_snapshot(
    api_client, idempotency_headers, create_namespace
):
    nodes = _seed_resource_tree(api_client, idempotency_headers, create_namespace)
    response = _activate_tree(
        api_client, idempotency_headers, nodes["root"], recursive_depth=None
    )
    assert response.status_code == 200, response.text
    root = command_model(response)
    child = root.children["child"]
    archived = api_client.post(
        f"/api/v1/lifecycle/resource/{child.id}",
        headers=idempotency_headers(
            "archive-after-finalize", **{"If-Match": f'"{child.revision}"'}
        ),
        json={"status": "ARCHIVED"},
    )
    assert archived.status_code == 200
    replay = _activate_tree(
        api_client, idempotency_headers, nodes["root"], recursive_depth=None
    )
    assert replay.status_code == 200
    assert replay.json() == response.json()
    conflicting = _activate_tree(
        api_client, idempotency_headers, nodes["root"], recursive_depth=1
    )
    assert conflicting.status_code == 409


@pytest.mark.parametrize(
    "object_type", ["resource_template", "process_template", "process_run"]
)
def test_rest_recursive_finalization_rejects_non_resources(
    api_client, idempotency_headers, create_namespace, object_type
):
    entity = _seed_lifecycle_entity(
        api_client, idempotency_headers, create_namespace, object_type
    )
    response = api_client.post(
        f"/api/v1/lifecycle/{object_type}/{command_data(entity)['id']}",
        headers=idempotency_headers("invalid-recursive", **{"If-Match": '"1"'}),
        json={"status": "ACTIVE", "recursive_depth": 1},
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("depth", [-1, True, False, 1.5, "2"])
def test_rest_finalization_rejects_invalid_depth(
    api_client, idempotency_headers, create_namespace, depth
):
    entity = _seed_lifecycle_entity(
        api_client, idempotency_headers, create_namespace, "resource"
    )
    response = _activate_tree(
        api_client,
        idempotency_headers,
        command_model(entity),
        recursive_depth=depth,
    )
    assert response.status_code == 422, response.text
