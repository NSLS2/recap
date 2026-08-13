from types import SimpleNamespace
from uuid import uuid4

from recap.commands.models import (
    CreateProcessRun,
    CreateProcessTemplate,
    CreateResource,
)
from recap.client.backend import ClientBackend
from recap.dsl.process_builder import ProcessRunBuilder, ProcessTemplateBuilder
from recap.dsl.resource_builder import ResourceBuilder, ResourceTemplateBuilder
from recap.schemas.resource import ResourceTemplateSchema
from recap.tests.transport_factories import resource_template
from recap.utils.general import Direction


class RecordingBackend:
    def __init__(self, existing=None):
        self.commands = []
        self.existing = existing
        self.resource_template = None

    def get_process_template(self, *args, **kwargs):
        return self.existing

    def find_resources_by_identity(self, *args, **kwargs):
        return []

    def query(self, schema, *args, **kwargs):
        if (
            schema is ResourceTemplateSchema
            and args
            and args[0].filters.get("name") is not None
        ):
            self.resource_template = resource_template().model_copy(
                update={
                    "name": args[0].filters["name"],
                    "version": args[0].filters["version"],
                }
            )
            return [self.resource_template]
        if schema is ResourceTemplateSchema and self.resource_template is not None:
            return [self.resource_template]
        if (
            schema.__name__ == "ProcessTemplateSchema"
            and args
            and args[0].filters.get("id") is not None
        ):
            return [
                SimpleNamespace(
                    id=uuid4(), step_templates={}, resource_slots=[]
                )
            ]
        return []

    def execute(self, command, context):
        self.commands.append((command, context))
        return self.existing

    def count(self, *args, **kwargs):
        return 0

    def list_child_namespaces(self, parent_path):
        return []

    def create_namespace(self, path, metadata, context):
        return None

    def update_namespace(
        self, namespace_id, expected_revision, metadata, status, context, *, etag=None
    ):
        return None


class RecordingReader:
    def __init__(self, existing=None):
        self.existing = existing
        self.queries = []

    def query(self, schema, spec, *, namespace_path):
        self.queries.append((schema, spec, namespace_path))
        if schema is ResourceTemplateSchema:
            template = self.existing or resource_template()
            if "name" in spec.filters:
                template = template.model_copy(
                    update={
                        "name": spec.filters["name"],
                        "version": spec.filters["version"],
                    }
                )
            return [template]
        return []

    def count(self, schema, spec, *, namespace_path):
        return 0


class RecordingWriter:
    def __init__(self, existing=None):
        self.existing = existing
        self.commands = []

    def execute(self, command, context):
        self.commands.append((command, context))
        return self.existing


class RecordingNamespaces:
    def list_child_namespaces(self, parent_path):
        return []


class RecordingNamespaceWriter:
    def create_namespace(self, path, metadata, context):
        return None

    def update_namespace(
        self, namespace_id, expected_revision, metadata, status, context, *, etag=None
    ):
        return None


def resource_backend():
    reader = RecordingReader()
    writer = RecordingWriter()
    return (
        ClientBackend(
            reader=reader,
            writer=writer,
            namespaces=RecordingNamespaces(),
            namespace_writer=RecordingNamespaceWriter(),
        ),
        reader,
        writer,
    )


def process_backend(adapter=None):
    adapter = adapter or RecordingBackend()
    return (
        ClientBackend(
            reader=adapter,
            writer=adapter,
            namespaces=adapter,
            namespace_writer=adapter,
        ),
        adapter,
    )


def test_process_template_body_has_no_backend_mutation():
    client_backend, backend = process_backend()
    context = object()
    builder = ProcessTemplateBuilder(
        backend=client_backend,
        namespace_id=uuid4(),
        namespace_path="beamline/amx",
        name="draft-template",
        version="1.0",
        command_context=context,
    )

    builder.add_resource_slot("input", "container", Direction.input)
    step = builder.add_step("measure")

    assert backend.commands == []
    assert step.parent.backend is client_backend


def test_process_run_exception_discards_draft():
    client_backend, backend = process_backend()
    builder = ProcessRunBuilder(
        name="run",
        description="desc",
        template_name="template",
        version="1.0",
        namespace_id=uuid4(),
        backend=client_backend,
        namespace_path="beamline/amx",
        template_id=uuid4(),
        command_context=object(),
    )

    try:
        with builder:
            builder.assign_resource("input", type("Resource", (), {"id": uuid4()})())
            raise RuntimeError("discard")
    except RuntimeError:
        pass

    assert backend.commands == []


def test_process_builders_submit_one_command_on_clean_exit():
    client_backend, backend = process_backend()
    context = object()
    with ProcessTemplateBuilder(
        backend=client_backend,
        namespace_id=uuid4(),
        namespace_path="beamline/amx",
        name="template",
        version="1.0",
        command_context=context,
    ) as builder:
        builder.add_step("measure")

    assert len(backend.commands) == 1
    assert isinstance(backend.commands[0][0], CreateProcessTemplate)


def test_process_run_builder_submits_one_aggregate_command():
    client_backend, backend = process_backend()
    builder = ProcessRunBuilder(
        name="run",
        description="desc",
        template_name="template",
        version="1.0",
        namespace_id=uuid4(),
        backend=client_backend,
        namespace_path="beamline/amx",
        template_id=uuid4(),
        command_context=object(),
    )

    builder.assign_resource("input", type("Resource", (), {"id": uuid4()})())
    builder.save()

    assert len(backend.commands) == 1
    assert isinstance(backend.commands[0][0], CreateProcessRun)


def test_resource_builder_has_no_construction_side_effect_and_submits_once():
    client_backend, reader, writer = resource_backend()
    builder = ResourceBuilder(
        name="resource",
        template_name="template",
        backend=client_backend,
        namespace_id=uuid4(),
        namespace_path="beamline/amx",
        command_context=object(),
    )

    assert writer.commands == []
    builder.save()

    assert reader.queries
    assert len(writer.commands) == 1
    assert isinstance(writer.commands[0][0], CreateResource)


def test_resource_builder_serializes_property_values_into_create_command():
    client_backend, _, writer = resource_backend()
    builder = ResourceBuilder(
        name="resource-with-values",
        template_name="template",
        backend=client_backend,
        namespace_id=uuid4(),
        namespace_path="beamline/amx",
        command_context=object(),
    )
    builder._resource = SimpleNamespace(
        properties={
            "measurements": SimpleNamespace(
                values=SimpleNamespace(
                    dose=SimpleNamespace(
                        value=12,
                        unit="mg",
                        metadata_json={"source": "builder"},
                    )
                )
            )
        }
    )
    builder.save()

    assert writer.commands[0][0].properties == {
        "measurements": {
            "dose": {
                "value": 12,
                "unit": "mg",
                "metadata_json": {"source": "builder"},
            }
        }
    }


def test_resource_child_builder_reuses_client_backend():
    client_backend, _, _ = resource_backend()
    builder = ResourceBuilder(
        name="parent",
        template_name="template",
        backend=client_backend,
        namespace_id=uuid4(),
        namespace_path="beamline/amx",
        command_context=object(),
    )

    child = builder.add_child("child", "template")

    assert child.backend is client_backend


def test_client_routes_local_builders_through_commands_without_begin(client):
    client.create_namespace("command")
    scoped = client.namespace("command")
    backend = scoped.backend

    with scoped.build_process_template("command-pt", "1.0"):
        pass
    with scoped.build_resource_template(name="command-rt", type_names=["container"]):
        pass

    assert not hasattr(backend, "begin")

    process_template = scoped.build_process_template("command-pt-2", "1.0")
    process_run = scoped.build_process_run(
        "command-run", "description", "command-pt", "1.0"
    )
    resource_template = scoped.build_resource_template(
        name="command-rt-2", type_names=["container"]
    )
    resource = scoped.build_resource(
        "command-resource", "command-rt", on_existing="create"
    )

    for builder in (process_template, process_run, resource_template, resource):
        assert builder._command_context is not None
    assert resource_template.backend is backend
    assert resource.backend is backend


def test_resource_template_command_draft_accepts_attribute_group_builder():
    client_backend, _, writer = resource_backend()
    builder = ResourceTemplateBuilder(
        name="template",
        type_names=["container"],
        backend=client_backend,
        namespace_id=uuid4(),
        namespace_path="beamline/amx",
        command_context=object(),
    )

    builder.prop_group("properties").add_attribute(
        "serial", "str", "", ""
    ).close_group()
    builder.save()

    assert writer.commands[0][0].draft.property_groups[0].attributes[0].name == "serial"


def test_process_run_command_builder_rejects_missing_template_and_run_ids():
    import pytest
    client_backend, backend = process_backend()

    with pytest.raises(ValueError, match="template_id or process_run_id"):
        ProcessRunBuilder(
            "run",
            "description",
            "template",
            uuid4(),
            backend=client_backend,
            namespace_path="beamline/amx",
            command_context=object(),
        )


def test_process_run_command_save_tolerates_none_or_partial_result():
    class ResultBackend(RecordingBackend):
        def __init__(self, result):
            super().__init__()
            self.result = result

        def execute(self, command, context):
            self.commands.append((command, context))
            return self.result

    for result in (None, object()):
        client_backend, backend = process_backend(ResultBackend(result))
        builder = ProcessRunBuilder(
            "run",
            "description",
            "template",
            uuid4(),
            backend=client_backend,
            namespace_path="beamline/amx",
            template_id=uuid4(),
            command_context=object(),
        )
        builder.save()

        assert len(backend.commands) == 1


def test_process_run_builder_loads_template_without_client_lookup():
    template_id = uuid4()

    class QueryBackend(RecordingBackend):
        def query(self, schema, spec, *, namespace_path):
            if spec.filters == {"name": "template", "version": "1.0"}:
                return [
                    SimpleNamespace(
                        id=template_id,
                        step_templates={},
                        resource_slots=[],
                    )
                ]
            return []

    client_backend, backend = process_backend(QueryBackend())
    builder = ProcessRunBuilder(
        "run",
        "description",
        "template",
        uuid4(),
        backend=client_backend,
        version="1.0",
        namespace_path="beamline/amx",
        command_context=object(),
    )

    assert builder._template_id == template_id


def test_process_run_command_save_handles_missing_template_steps():
    client_backend, backend = process_backend()
    builder = ProcessRunBuilder(
        "run",
        "description",
        "template",
        uuid4(),
        backend=client_backend,
        namespace_path="beamline/amx",
        template_id=uuid4(),
        command_context=object(),
    )
    builder._process_template = SimpleNamespace(step_templates=None)

    builder.save()

    assert len(backend.commands) == 1


def test_resource_reuse_with_changed_properties_submits_update(client):
    with client.build_resource_template(
        name="ReuseUpdateRT", type_names=["container"]
    ) as template:
        template.prop_group("properties").add_attribute(
            "serial", "str", "", ""
        ).close_group()
    first = client.create_resource("ReuseUpdate", "ReuseUpdateRT", on_existing="create")

    with client.build_resource(
        "ReuseUpdate", "ReuseUpdateRT", on_existing="silent"
    ) as builder:
        builder.resource.properties["properties"].values["serial"] = "changed"

    with client.build_resource(resource_id=first.id) as verifier:
        assert (
            verifier.resource.properties["properties"].values.serial.value == "changed"
        )
