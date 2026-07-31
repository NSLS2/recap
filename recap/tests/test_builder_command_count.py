from types import SimpleNamespace
from uuid import uuid4

from recap.commands.models import UpdateProcessTemplate
from recap.dsl.process_builder import ProcessTemplateBuilder


class RecordingBackend:
    def __init__(self, existing):
        self.existing = existing
        self.commands = []

    def get_process_template(self, *args, **kwargs):
        return self.existing

    def execute(self, command, context):
        self.commands.append(command)
        values = vars(self.existing).copy()
        values["revision"] += 1
        self.existing = SimpleNamespace(**values)
        return self.existing


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
        backend=backend,
        namespace_id=uuid4(),
        namespace_path="beamline/amx",
        name=None,
        version=None,
        process_template_id=existing.id,
        command_context=context,
    )

    builder.save()
    builder.save()

    assert len(backend.commands) == 2
    assert all(isinstance(command, UpdateProcessTemplate) for command in backend.commands)
    assert backend.commands[0].expected_revision == 3
    assert backend.commands[1].expected_revision == 4
