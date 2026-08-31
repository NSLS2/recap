"""Remote command writes and REST read parity."""

from unittest.mock import patch
from uuid import uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient

from recap.adapter.rest import RESTAdapter
from recap.client import RecapClient
from recap.exceptions import (
    RecapAuthenticationError,
    RecapConflictError,
    RecapNotFoundError,
)
from recap.schemas.namespace import NamespaceContext
from recap.schemas.resource import ResourceCopyOptions
from recap.server.app import create_app


@pytest.fixture(params=["local", "remote"], ids=["local", "remote"])
def command_client(request, tmp_path, rest_loopback_client):
    db_path = tmp_path / f"{request.param}-commands.db"
    if request.param == "local":
        with RecapClient.from_sqlite(db_path) as client:
            yield client
        return

    yield rest_loopback_client


def seed_command_namespace(client):
    client.create_namespace("beamline")
    client.create_namespace("beamline/amx")
    scoped = client.namespace("beamline/amx")
    return scoped


def test_resource_template_lifecycle_has_local_remote_parity(command_client):
    scoped = seed_command_namespace(command_client)
    with scoped.build_resource_template(
        name="Sample", type_names=["sample"]
    ) as builder:
        pass

    builder.activate()
    assert builder.template.status.value == "ACTIVE"
    builder.archive()
    assert builder.template.status.value == "ARCHIVED"


def test_namespace_update_has_local_remote_parity(command_client):
    scoped = seed_command_namespace(command_client)

    updated = scoped.update_namespace(metadata={"beamline": "AMX"})

    assert updated.metadata == {"beamline": "AMX"}
    assert updated.revision == 2


def test_resource_template_load_update_has_local_remote_parity(command_client):
    scoped = seed_command_namespace(command_client)
    with scoped.build_resource_template(
        name="Sample", type_names=["sample"]
    ) as created:
        pass

    with scoped.build_resource_template(
        resource_template_id=created.template.id
    ) as loaded:
        loaded.prop_group("details").add_attribute(
            "batch", "str", "", "B-1"
        ).close_group()

    assert (
        loaded.template.attribute_group_templates[0]
        .attribute_templates[0]
        .default_value
        == "B-1"
    )
    assert loaded.template.revision == 2


def test_resource_update_has_local_remote_parity(command_client):
    scoped = seed_command_namespace(command_client)
    with scoped.build_resource_template(
        name="Sample", type_names=["sample"]
    ) as template:
        template.prop_group("details").add_attribute(
            "serial", "str", "", "old"
        ).close_group()
    resource = scoped.create_resource("sample-1", "Sample")

    with scoped.build_resource(resource_id=resource.id) as builder:
        assert builder.resource.id == resource.id
        model = builder.get_model()
        model.properties.details.values.serial.value = "new"
        builder.set_model(model)

    assert builder.resource.properties.details.values.serial.value == "new"
    assert builder.resource.revision == 2


def test_resource_copy_has_local_remote_parity(command_client):
    scoped = seed_command_namespace(command_client)
    with scoped.build_resource_template(name="Sample", type_names=["sample"]):
        pass
    source = scoped.create_resource("sample-1", "Sample")

    copied = scoped.copy_resource(source.id)

    assert copied.id != source.id
    assert copied.template.id == source.template.id


def test_resource_copy_as_child_has_local_remote_parity(command_client):
    scoped = seed_command_namespace(command_client)
    with scoped.build_resource_template(name="Group", type_names=["group"]):
        pass
    with scoped.build_resource_template(name="Sample", type_names=["sample"]):
        pass
    group = scoped.create_resource("group-1", "Group")
    source = scoped.create_resource("sample-1", "Sample")
    source_child = scoped.create_resource("sample-child", "Sample", parent=source)

    with scoped.build_resource(resource_id=group.id) as builder:
        copied = builder.add_child(source)

    assert copied.resource.id != source.id
    assert copied.resource.copied_from_id == source.id
    copied_descendant = copied.resource.children["sample-child"]
    assert copied_descendant.id != source_child.id
    assert all(
        descendant.copied_from_id is None
        for descendant in copied.resource.children.values()
    )
    with scoped.build_resource(resource_id=group.id) as loader:
        refreshed = loader.get_model(update=True)
    assert refreshed.children["sample-1"].id == copied.resource.id
    assert refreshed.children["sample-1"].copied_from_id == source.id


def test_resource_lifecycle_has_local_remote_parity(command_client):
    scoped = seed_command_namespace(command_client)
    with scoped.build_resource_template(name="Sample", type_names=["sample"]):
        pass
    resource = scoped.create_resource("sample-1", "Sample")

    with scoped.build_resource(resource_id=resource.id) as builder:
        builder.activate()
        assert builder.resource.status.value == "ACTIVE"
        builder.archive()
        assert builder.resource.status.value == "ARCHIVED"


def test_process_template_create_load_update_lifecycle_has_local_remote_parity(
    command_client,
):
    scoped = seed_command_namespace(command_client)
    with scoped.build_process_template("Measure", "1.0") as created:
        created.add_step("Collect").close_step()

    with scoped.build_process_template(
        process_template_id=created.template.id
    ) as loaded:
        loaded.add_step("Analyze").close_step()

    assert loaded.template.revision == 2
    assert {step.name for step in loaded.template.step_templates.values()} == {
        "Collect",
        "Analyze",
    }
    loaded.activate()
    assert loaded.template.status.value == "ACTIVE"
    loaded.archive()
    assert loaded.template.status.value == "ARCHIVED"


def test_process_run_create_load_update_lifecycle_has_local_remote_parity(
    command_client,
):
    scoped = seed_command_namespace(command_client)
    with scoped.build_process_template("Measure", "1.0") as template:
        (
            template.add_step("Mix")
            .param_group("Inputs")
            .add_attribute("Voltage", "int", "", 0)
            .close_group()
            .close_step()
        )

    with scoped.build_process_run(
        name="run-1",
        description="initial",
        template_name="Measure",
        version="1.0",
    ) as created:
        run_id = created.process_run.id

    with scoped.build_process_run(process_run_id=run_id) as loaded:
        model = loaded.get_model()
        model.description = "updated"
        loaded.set_model(model)

    assert loaded.process_run.description == "updated"
    assert loaded.process_run.revision == 2
    loaded.finalize()
    assert loaded.process_run.status.value == "ACTIVE"
    loaded.archive()
    assert loaded.process_run.status.value == "ARCHIVED"


def test_stale_resource_write_preserves_first_mutation(command_client):
    scoped = seed_command_namespace(command_client)
    with scoped.build_resource_template(name="Sample", type_names=["sample"]):
        pass
    resource = scoped.create_resource("sample-1", "Sample")

    first = scoped.build_resource(resource_id=resource.id)
    second = scoped.build_resource(resource_id=resource.id)
    first_model = first.get_model()
    first_model.name = "first"
    first.set_model(first_model)
    first.save()

    second_model = second.get_model()
    second_model.name = "second"
    second.set_model(second_model)
    with pytest.raises(RecapConflictError):
        second.save()

    with scoped.build_resource(resource_id=resource.id) as persisted:
        assert persisted.resource.name == "first"


def test_remote_writes_are_visible_to_rest_queries(tmp_path):
    db_path = tmp_path / "remote.db"
    api_key = "remote-secret"
    app_client = TestClient(create_app(db_path, api_key=api_key))

    def request(_client, method, url, **kwargs):
        path = url.removeprefix("http://recap.test")
        response = app_client.request(method, path, **kwargs)
        return response

    with (
        patch.object(httpx2.Client, "request", request),
        RecapClient.from_url("http://recap.test", api_key=api_key) as remote,
    ):
        remote.create_namespace("beamline")
        remote.create_namespace("beamline/amx", metadata={"beamline": "amx"})
        namespace = remote.namespace("beamline/amx")

        with namespace.build_resource_template(name="Sample", type_names=["sample"]):
            pass
        with namespace.build_process_template("Measure", "1.0"):
            pass
        resource = namespace.create_resource("S-001", "Sample")
        copied = namespace.copy_resource(
            resource.id,
            ResourceCopyOptions(name="S-001-copy"),
        )
        with namespace.build_resource(resource_id=copied.id) as copied_builder:
            copied_builder.activate()
        with namespace.build_process_run(
            name="run-001",
            description="remote run",
            template_name="Measure",
            version="1.0",
        ) as process_run_builder:
            process_run = process_run_builder.process_run
        process_run_builder.finalize()

        assert namespace.namespace_path == "beamline/amx"
        assert copied.name == "S-001-copy"
        assert process_run.name == "run-001"
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
    with patch.object(
        RESTAdapter,
        "get_namespace_context",
        return_value=NamespaceContext(id=uuid4(), path="beamline/amx"),
    ):
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

    with (
        patch.object(httpx2.Client, "request", request),
        RecapClient.from_url("http://recap.test", api_key=api_key) as remote,
    ):
        remote.create_namespace("beamline")
        remote.create_namespace("beamline/amx")
        namespace = remote.namespace("beamline/amx")

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

    with (
        patch.object(httpx2.Client, "request", request),
        RecapClient.from_url("http://recap.test", api_key=api_key) as remote,
    ):
        remote.create_namespace("beamline")
        remote.create_namespace("beamline/amx")
        namespace = remote.namespace("beamline/amx")
        with namespace.build_resource_template(name="Sample", type_names=["sample"]):
            pass

        resource = namespace.create_resource("S-001", "Sample")
        query_paths = []

        def query(_adapter, schema, spec, *, namespace_path):
            query_paths.append(namespace_path)
            return [resource]

        with (
            patch.object(RESTAdapter, "query", query),
            namespace.build_resource(resource_id=resource.id) as builder,
        ):
            assert builder.resource.id == resource.id
        assert query_paths == ["beamline/amx"]

        assert resource.name == "S-001"
        assert ("POST", "/api/v1/resources/beamline/amx") in request_paths
        namespace.close()


def test_remote_resource_builder_reports_missing_template_without_lookup(tmp_path):
    db_path = tmp_path / "remote-missing-template.db"
    api_key = "remote-secret"
    app_client = TestClient(create_app(db_path, api_key=api_key))

    def request(_client, method, url, **kwargs):
        path = url.removeprefix("http://recap.test")
        return app_client.request(method, path, **kwargs)

    with (
        patch.object(httpx2.Client, "request", request),
        patch.object(RESTAdapter, "query", return_value=[]),
        RecapClient.from_url("http://recap.test", api_key=api_key) as remote,
    ):
        remote.create_namespace("beamline")
        remote.create_namespace("beamline/amx")
        namespace = remote.namespace("beamline/amx")

        with pytest.raises(
            RecapNotFoundError,
            match="Resource template 'Missing' version '1.0' not found",
        ):
            namespace.build_resource("S-001", "Missing")

        namespace.close()


def test_remote_namespace_write_rejects_invalid_api_key(tmp_path, monkeypatch):
    with TestClient(
        create_app(tmp_path / "auth.db", api_key="correct-key")
    ) as app_client:

        def request(_client, method, url, **kwargs):
            response = app_client.request(
                method, url.removeprefix("http://recap.test"), **kwargs
            )
            request = httpx2.Request(method, url, headers=kwargs.get("headers"))
            return httpx2.Response(
                response.status_code,
                headers=response.headers,
                content=response.content,
                request=request,
            )

        monkeypatch.setattr(
            httpx2.Client,
            "request",
            request,
        )
        with (
            patch.object(
                RESTAdapter,
                "get_namespace_context",
                return_value=NamespaceContext(id=uuid4(), path=""),
            ),
            RecapClient.from_url("http://recap.test", api_key="wrong-key") as client,
            pytest.raises(RecapAuthenticationError) as caught,
        ):
            client.create_namespace("beamline")

    assert caught.value.status_code == 401
    assert "wrong-key" not in str(caught.value)
