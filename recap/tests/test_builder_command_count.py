from types import SimpleNamespace
from uuid import uuid4

from recap.commands.models import UpdateProcessTemplate
from recap.client.backend import ClientBackend
from recap.dsl.process_builder import ProcessTemplateBuilder


class RecordingBackend:
    def __init__(self, existing):
        self.existing = existing
        self.commands = []

    def get_process_template(self, *args, **kwargs):
        return self.existing

    def query(self, schema, spec, *, namespace_path):
        return [self.existing] if "id" in spec.filters else []

    def execute(self, command, context):
        self.commands.append(command)
        values = vars(self.existing).copy()
        values["revision"] += 1
        self.existing = SimpleNamespace(**values)
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


def client_backend(adapter):
    return ClientBackend(
        reader=adapter,
        writer=adapter,
        namespaces=adapter,
        namespace_writer=adapter,
    )


def test_repeated_save_uses_latest_revision():
    context = object()
    existing = SimpleNamespace(
        id=uuid4(),
        name="repeat",
        version="1.0",
        revision=3,
        labels=[],
        resource_slots=[],
        step_templates={},
    )
    backend = RecordingBackend(existing)
    builder = ProcessTemplateBuilder(
        backend=client_backend(backend),
        namespace_id=uuid4(),
        namespace_path="beamline/amx",
        name=None,
        version=None,
        process_template_id=existing.id,
        command_context=context,
    )

    builder.save()
    builder.add_resource_slot("input", "container", "input")
    builder.save()

    assert len(backend.commands) == 2
    assert all(
        isinstance(command, UpdateProcessTemplate) for command in backend.commands
    )
    assert backend.commands[0].expected_revision == 3
    assert backend.commands[1].expected_revision == 4


def test_clean_repeated_save_does_not_duplicate_command():
    existing = SimpleNamespace(
        id=uuid4(),
        name="clean",
        version="1.0",
        revision=3,
        labels=[],
        resource_slots=[],
        step_templates={},
    )
    backend = RecordingBackend(existing)
    builder = ProcessTemplateBuilder(
        backend=client_backend(backend),
        namespace_id=uuid4(),
        namespace_path="beamline/amx",
        name=None,
        version=None,
        process_template_id=existing.id,
        command_context=object(),
    )

    builder.save()
    builder.save()

    assert len(backend.commands) == 1
