"""Remote command writes and GraphQL read parity."""

from unittest.mock import patch

import httpx2
from fastapi.testclient import TestClient

from recap.client import RecapClient
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
        namespace_result = namespace.create(metadata={"beamline": "amx"})

        resource_template = remote.backend.create(
            "resource-templates",
            namespace.path,
            {
                "name": "Sample",
                "version": "1.0",
                "type_names": ["sample"],
            },
        ).entity
        process_template = remote.backend.create(
            "process-templates",
            namespace.path,
            {"name": "Measure", "version": "1.0"},
        ).entity
        resource = remote.backend.create(
            "resources",
            namespace.path,
            {"name": "S-001", "template_id": resource_template["id"]},
        ).entity
        copied = remote.backend.copy_resource(
            resource["id"],
            namespace.path,
            changes={"name": "S-001-copy", "changes": {"properties": {}}},
        ).entity
        process_run = remote.backend.create(
            "process-runs",
            namespace.path,
            {
                "name": "run-001",
                "description": "remote run",
                "template_id": process_template["id"],
                "assignments": {},
                "steps": {},
            },
        ).entity

        assert namespace_result.path == namespace.path
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
