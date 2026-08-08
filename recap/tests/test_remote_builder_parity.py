"""Remote command writes and GraphQL read parity."""

from unittest.mock import patch

import httpx2
from fastapi.testclient import TestClient

from recap.adapter.graphql import GraphQLAdapter
from recap.client import RecapClient
from recap.schemas.resource import ResourceTemplateSchema
from recap.server.app import create_app


def test_remote_writes_are_visible_to_graphql_and_match_rest_entities(tmp_path):
    db_path = tmp_path / "remote.db"
    api_key = "remote-secret"
    app_client = TestClient(create_app(db_path, api_key=api_key))

    def request(_client, method, url, **kwargs):
        path = url.removeprefix("http://recap.test")
        response = app_client.request(method, path, **kwargs)
        return response

    def post(_client, url, **kwargs):
        return app_client.post("/graphql", **kwargs)

    with (
        patch.object(httpx2.Client, "request", request),
        patch.object(httpx2.Client, "post", post),
        RecapClient.from_url("http://recap.test", api_key=api_key) as remote,
    ):
        namespace = remote.namespace("beamline/amx")
        namespace_result = namespace.create_namespace(
            namespace.namespace_path, metadata={"beamline": "amx"}
        )

        resource_template = namespace.backend.create(
            "resource-templates",
            namespace.namespace_path,
            {
                "name": "Sample",
                "version": "1.0",
                "type_names": ["sample"],
            },
        ).entity
        process_template = namespace.backend.create(
            "process-templates",
            namespace.namespace_path,
            {"name": "Measure", "version": "1.0"},
        ).entity
        resource = namespace.backend.create(
            "resources",
            namespace.namespace_path,
            {"name": "S-001", "template_id": resource_template["id"]},
        ).entity
        copied = namespace.backend.copy_resource(
            resource["id"],
            namespace.namespace_path,
            changes={"name": "S-001-copy", "changes": {"properties": {}}},
        ).entity
        process_run = namespace.backend.create(
            "process-runs",
            namespace.namespace_path,
            {
                "name": "run-001",
                "description": "remote run",
                "template_id": process_template["id"],
                "assignments": {},
                "steps": {},
            },
        ).entity

        assert namespace_result.path == namespace.namespace_path
        assert copied["name"] == "S-001-copy"
        assert process_run["name"] == "run-001"
        assert [item.name for item in namespace.query_maker().resources().all()] == [
            "S-001",
            "S-001-copy",
        ]
        assert "Sample" in [
            item.name for item in namespace.query_maker().resource_templates().all()
        ]
        assert "Measure" in [
            item.name for item in namespace.query_maker().process_templates().all()
        ]
        assert [item.name for item in namespace.query_maker().process_runs().all()] == [
            "run-001"
        ]


def test_scoped_remote_query_uses_view_namespace():
    client = RecapClient.from_url("http://recap.test", api_key="secret")
    scoped = client.namespace("beamline/amx")

    query = scoped.query_maker().resources()

    assert query._context.path == "beamline/amx"
    scoped.close()
    client.close()


def test_scoped_remote_public_builders_use_namespace_routes(tmp_path):
    db_path = tmp_path / "remote-builders.db"
    api_key = "remote-secret"
    app_client = TestClient(create_app(db_path, api_key=api_key))
    request_paths = []
    template_entity = None

    def request(_client, method, url, **kwargs):
        nonlocal template_entity
        path = url.removeprefix("http://recap.test")
        request_paths.append((method, path))
        response = app_client.request(method, path, **kwargs)
        if path == "/api/v1/resource-templates/beamline/amx":
            template_entity = response.json()
        return response

    def post(_client, url, **kwargs):
        return app_client.post("/graphql", **kwargs)

    with (
        patch.object(httpx2.Client, "request", request),
        patch.object(httpx2.Client, "post", post),
        patch.object(
            GraphQLAdapter,
            "get_resource_template",
            side_effect=lambda *args, **kwargs: ResourceTemplateSchema.model_validate(
                template_entity
            ),
        ),
        patch.object(GraphQLAdapter, "find_resources_by_identity", return_value=[]),
        RecapClient.from_url("http://recap.test", api_key=api_key) as remote,
    ):
        namespace = remote.namespace("beamline/amx")
        namespace.create_namespace(namespace.namespace_path)

        with namespace.build_resource_template(name="Sample", type_names=["sample"]):
            pass

        with namespace.build_resource("S-001", "Sample") as resource_builder:
            resource_id = resource_builder.resource.id

        assert resource_id
        assert ("POST", "/api/v1/resource-templates/beamline/amx") in request_paths
        assert ("POST", "/api/v1/resources/beamline/amx") in request_paths

        namespace.close()


def test_scoped_remote_create_resource_uses_namespace_route(tmp_path):
    db_path = tmp_path / "remote-create-resource.db"
    api_key = "remote-secret"
    app_client = TestClient(create_app(db_path, api_key=api_key))
    request_paths = []
    template_entity = None

    def request(_client, method, url, **kwargs):
        nonlocal template_entity
        path = url.removeprefix("http://recap.test")
        request_paths.append((method, path))
        response = app_client.request(method, path, **kwargs)
        if path == "/api/v1/resource-templates/beamline/amx":
            template_entity = response.json()
        return response

    def post(_client, url, **kwargs):
        return app_client.post("/graphql", **kwargs)

    with (
        patch.object(httpx2.Client, "request", request),
        patch.object(httpx2.Client, "post", post),
        patch.object(
            GraphQLAdapter,
            "get_resource_template",
            side_effect=lambda *args, **kwargs: ResourceTemplateSchema.model_validate(
                template_entity
            ),
        ),
        patch.object(GraphQLAdapter, "find_resources_by_identity", return_value=[]),
        RecapClient.from_url("http://recap.test", api_key=api_key) as remote,
    ):
        namespace = remote.namespace("beamline/amx")
        namespace.create_namespace(namespace.namespace_path)
        with namespace.build_resource_template(name="Sample", type_names=["sample"]):
            pass

        resource = namespace.create_resource("S-001", "Sample")

        assert resource.name == "S-001"
        assert ("POST", "/api/v1/resources/beamline/amx") in request_paths
        namespace.close()
