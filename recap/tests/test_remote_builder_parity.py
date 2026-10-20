"""Remote command writes and REST read parity."""

from unittest.mock import patch
from uuid import uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient

from recap.adapter.rest import RESTAdapter
from recap.client import RecapClient
from recap.dsl.query import QuerySpec
from recap.exceptions import (
    RecapAuthenticationError,
    RecapConflictError,
    RecapNotFoundError,
)
from recap.schemas.namespace import NamespaceContext
from recap.schemas.process import ProcessTemplateSchema
from recap.schemas.resource import ResourceCopyOptions, ResourceTemplateSchema
from recap.server.app import create_app
from recap.utils.general import Direction


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


def test_overlapping_trees_and_assignments_preserve_generated_properties(
    command_client,
):
    scoped = seed_command_namespace(command_client)
    with scoped.build_resource_template(name="Overlap plate", type_names=["plate"]):
        pass
    for index in range(2):
        with scoped.build_resource_template(
            name=f"Overlap well {index}", type_names=["well"]
        ) as template:
            template.add_properties(
                {
                    "Measurements": [
                        {
                            "name": "Dose",
                            "type": "int",
                            "default": None,
                            "unit": "uL",
                            "metadata": {"min": 0, "max": 10},
                        },
                        {"name": "Label", "type": "str", "default": f"well-{index}"},
                        {"name": "Readings", "type": "array", "default": [index]},
                    ]
                }
            )
    with scoped.build_resource("plate", "Overlap plate") as plate_builder:
        for index in range(2):
            plate_builder.add_child(f"well-{index}", f"Overlap well {index}")
    with plate_builder:
        plate_builder.finalize()
    for child in plate_builder.resource.children.values():
        with scoped.build_resource(resource_id=child.id) as child_builder:
            child_builder.finalize()
    with scoped.build_process_template("Overlap measurement", "1.0") as template:
        template.add_resource_slot("plate", "plate", Direction.input)
        template.add_resource_slot("well", "well", Direction.input)
    plate = plate_builder.resource
    with scoped.build_process_run(
        "overlap-run", "", "Overlap measurement", "1.0"
    ) as run_builder:
        run_builder.assign_resource("plate", plate)
        run_builder.assign_resource("well", plate.children["well-0"])
    with run_builder:
        run_builder.finalize()

    maker = scoped.query_maker()
    tree_query = maker.resources(load="eager").filter(id=plate.id)
    tree = tree_query.first()
    snapshots = {}
    for name, child in tree.children.items():
        group = child.properties.measurements
        snapshots[name] = (
            child,
            child.properties,
            group,
            group.values,
            group.values.dose,
        )

    def assert_child(child):
        original, properties, group, values, dose = snapshots[child.name]
        assert child is original
        assert child.properties is properties
        assert type(child.properties) is type(properties)
        assert child.properties.measurements is group
        assert child.properties.measurements.values is values
        assert group.values.dose is dose
        assert dose.value is None
        assert dose.unit == "uL"
        assert group.values.label.value == child.name
        assert group.values.readings.value == [int(child.name[-1])]
        assert set(type(properties).model_fields) == {"measurements"}
        assert set(type(values).model_fields) == {"dose", "label", "readings"}

    for _ in range(2):
        run = (
            maker.process_runs(load="eager")
            .filter(id=run_builder.process_run.id)
            .first()
        )
        assigned_plate = run.assigned_resources["plate"].resource
        assert assigned_plate is tree
        assert_child(run.assigned_resources["well"].resource)
        for child in assigned_plate.children.values():
            assert_child(child)
        partial = maker.resources().filter(id=plate.id).first()
        assert partial is tree
        repeated = tree_query.all()
        assert repeated == [tree]
        for child in repeated[0].children.values():
            assert_child(child)
        wells = [
            resource
            for resource in maker.resources(load="eager").all()
            if resource.id != tree.id
        ]
        for resource in wells:
            assert_child(resource)


@pytest.mark.parametrize("kind", ["resource", "process"])
@pytest.mark.parametrize("null_default", [False, True], ids=["clear", "default"])
def test_null_values_round_trip_save_query_reopen(
    command_client, tmp_path, kind, null_default
):
    scoped = seed_command_namespace(command_client)
    defaults = {
        "int": 0,
        "float": 0.0,
        "bool": False,
        "str": "None",
        "datetime": "2024-01-01T00:00:00",
        "array": [],
        "enum": "known",
    }
    definitions = [
        {
            "name": f"value_{value_type}",
            "type": value_type,
            "default": None if null_default else default,
            "unit": "unit",
            "metadata": (
                {"choices": ["known"]}
                if value_type == "enum"
                else {"min": 0, "max": 10}
                if value_type in {"int", "float"}
                else {}
            ),
        }
        for value_type, default in defaults.items()
    ]
    if kind == "resource":
        with scoped.build_resource_template(
            name="Nullable", type_names=["sample"]
        ) as template:
            template.add_properties({"Measurements": definitions})
        group_template = template.template.attribute_group_templates[0]
        with scoped.build_resource("sample", "Nullable") as builder:
            pass
        instance = builder.resource
    else:
        with scoped.build_process_template("Nullable", "1.0") as template:
            (
                template.add_step("Collect")
                .add_parameters({"Measurements": definitions})
                .close_step()
            )
        group_template = template.template.step_templates[
            "Collect"
        ].attribute_group_templates[0]
        with scoped.build_process_run("run", "", "Nullable", "1.0") as builder:
            pass
        instance = builder.get_model(update=True)

    if null_default:
        assert all(
            attribute.default_value is None
            for attribute in group_template.attribute_templates
        )

    def values_of(model):
        return (
            model.properties["Measurements"].values
            if kind == "resource"
            else model.steps["Collect"].parameters["Measurements"].values
        )

    if not null_default:
        with builder:
            model = (
                builder.get_model()
                if kind == "resource"
                else builder.get_params("Collect")
            )
            values = (
                values_of(model) if kind == "resource" else model["Measurements"].values
            )
            for definition in definitions:
                attribute = getattr(values, definition["name"])
                assert attribute.value is not None
                attribute.value = None
                attribute.unit = "override"
            if kind == "resource":
                builder.set_model(model)
            else:
                builder.set_params(model)
        instance = builder.resource if kind == "resource" else builder.process_run

    def assert_nulls(model):
        assert model.id == instance.id
        for definition in definitions:
            attribute = getattr(values_of(model), definition["name"])
            assert attribute.value is None
            assert attribute.unit == ("unit" if null_default else "override")

    assert_nulls(instance)
    with builder:
        builder.finalize()

    def query(client):
        maker = client.namespace("beamline/amx").query_maker()
        family = (
            maker.resources(load="eager")
            if kind == "resource"
            else maker.process_runs(load="eager")
        )
        results = family.filter(id=instance.id).all()
        assert len(results) == 1
        return results[0]

    first = query(command_client)
    assert_nulls(first)
    second = query(command_client)
    assert_nulls(second)
    assert second is first

    command_client.close()
    db_path = tmp_path / (
        "local-commands.db"
        if (tmp_path / "local-commands.db").exists()
        else "rest-loopback.db"
    )
    with RecapClient.from_sqlite(db_path) as reopened:
        assert_nulls(query(reopened))


def make_template_builder(client, kind, name="Deferred"):
    scoped = seed_command_namespace(client)
    if kind == "process":
        return scoped.build_process_template(name, "1.0")
    return scoped.build_resource_template(name=name, type_names=["sample"])


def make_persisted_template_builder(client, kind):
    builder = make_template_builder(client, kind, name=f"Persisted-{kind}")
    with builder:
        pass
    return builder


def template_exists(client, kind, template_id):
    schema = ProcessTemplateSchema if kind == "process" else ResourceTemplateSchema
    return bool(
        client.connection_state.backend.query(
            schema,
            QuerySpec(filters={"id": template_id}, include_mutable=True),
            namespace_path="beamline/amx",
        )
    )


def test_resource_template_lifecycle_has_local_remote_parity(command_client):
    scoped = seed_command_namespace(command_client)
    with scoped.build_resource_template(
        name="Sample", type_names=["sample"]
    ) as builder:
        pass

    with builder:
        builder.finalize()
    assert builder.template.status.value == "ACTIVE"
    with builder:
        builder.archive()
    assert builder.template.status.value == "ARCHIVED"


@pytest.mark.parametrize("kind", ["process", "resource"])
def test_template_changes_are_local_until_clean_context_exit(command_client, kind):
    builder = make_template_builder(command_client, kind)
    if kind == "process":
        builder.add_step("Collect")
    else:
        builder.prop_group("meta").add_attribute("code", "str", "", "").close_group()
    assert builder.changes().fields
    assert not template_exists(command_client, kind, builder.template.id)
    with builder:
        pass
    assert template_exists(command_client, kind, builder.template.id)


@pytest.mark.parametrize("kind", ["process", "resource"])
def test_template_finalization_is_deferred_until_clean_exit(command_client, kind):
    builder = make_persisted_template_builder(command_client, kind)
    with builder:
        builder.finalize()
        assert builder.changes().lifecycle.value == "ACTIVE"
        assert builder.template.status.value == "MUTABLE"
    assert builder.template.status.value == "ACTIVE"


@pytest.mark.parametrize("kind", ["process", "resource"])
def test_template_nested_context_commits_once_and_exception_can_retry(
    command_client, kind
):
    builder = make_template_builder(
        client=command_client, kind=kind, name=f"Nested-{kind}"
    )
    if kind == "process":
        builder.add_step("Collect")
    else:
        builder.prop_group("meta").add_attribute("code", "str", "", "").close_group()
    with builder:  # noqa
        with builder:
            pass
    revision = builder.template.revision
    with pytest.raises(RuntimeError):  # noqa
        with builder:
            builder.finalize()
            raise RuntimeError("retry")
    assert builder.template.status.value == "MUTABLE"
    with builder:
        pass
    assert builder.template.status.value == "ACTIVE"
    assert builder.template.revision == revision + 1


def test_new_builders_keep_provisional_ids_after_commit(command_client):
    scoped = seed_command_namespace(command_client)
    with scoped.build_process_template("ID PT", "1.0") as process_template:
        process_template_id = process_template.template.id
    assert process_template.template.id == process_template_id

    with scoped.build_resource_template(
        name="ID RT", type_names=["sample"]
    ) as resource_template:
        resource_template_id = resource_template.template.id
    assert resource_template.template.id == resource_template_id

    process_run = scoped.build_process_run("ID run", "", "ID PT", "1.0")
    process_run_id = process_run.process_run.id
    with process_run:
        pass
    assert process_run.process_run.id == process_run_id

    resource = scoped.build_resource("ID resource", "ID RT")
    resource_id = resource.resource.id
    with resource:
        pass
    assert resource.resource.id == resource_id


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
    with scoped.build_resource(resource_id=source.id) as source_builder:
        source_child_builder = source_builder.add_child("sample-child", "Sample")
    source_child = source_child_builder.resource

    with scoped.build_resource(resource_id=group.id) as builder:
        copied = builder.add_child(source)

    assert copied.resource.id != source.id
    assert copied.resource.copied_from_id == source.id
    copied_descendant = copied.resource.children["sample-child"]
    assert copied_descendant.id != source_child.id
    assert copied_descendant.copied_from_id is None
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
        builder.finalize()
    assert builder.resource.status.value == "ACTIVE"
    with builder:
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
    with loaded:
        loaded.finalize()
    assert loaded.template.status.value == "ACTIVE"
    with loaded:
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
    with loaded:
        loaded.finalize()
    assert loaded.process_run.status.value == "ACTIVE"
    with loaded:
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
    with first:
        pass

    second_model = second.get_model()
    second_model.name = "second"
    second.set_model(second_model)
    with pytest.raises(RecapConflictError):  # noqa
        with second:
            pass

    assert second.resource.name == "second"
    assert second.changes().fields["name"] == "second"

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
            copied_builder.finalize()
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


def test_resource_in_place_edit_has_local_remote_parity(command_client):
    scoped = seed_command_namespace(command_client)
    with scoped.build_resource_template(
        name="Sample", type_names=["sample"]
    ) as template:
        template.prop_group("details").add_attribute(
            "serial", "str", "", "old"
        ).close_group()
    resource = scoped.create_resource("sample-1", "Sample")
    with scoped.build_resource(resource_id=resource.id) as builder:
        builder.finalize()
    active_revision = builder.resource.revision

    with scoped.build_resource(resource_id=resource.id, in_place=True) as builder:
        model = builder.get_model()
        model.properties.details.values.serial.value = "new"
        builder.set_model(model)

    assert builder.resource.id == resource.id
    assert builder.resource.status.value == "ACTIVE"
    assert builder.resource.revision == active_revision + 1
    assert builder.resource.properties.details.values.serial.value == "new"


def test_process_run_in_place_edit_has_local_remote_parity(command_client):
    scoped = seed_command_namespace(command_client)
    with scoped.build_process_template("Measure", "1.0"):
        pass
    with scoped.build_process_run("run-1", "original", "Measure", "1.0") as builder:
        builder.finalize()
    run = builder.process_run

    with scoped.build_process_run(process_run_id=run.id, in_place=True) as builder:
        model = builder.get_model()
        model.description = "corrected"
        builder.set_model(model)

    assert builder.process_run.id == run.id
    assert builder.process_run.status.value == "ACTIVE"
    assert builder.process_run.description == "corrected"
    assert builder.process_run.revision == run.revision + 1
