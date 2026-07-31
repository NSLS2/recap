from uuid import uuid4

from recap.commands.models import (
    CreateProcessRun,
    CreateProcessTemplate,
    CreateResource,
)
from recap.dsl.process_builder import ProcessRunBuilder, ProcessTemplateBuilder
from recap.dsl.resource_builder import ResourceBuilder
from recap.utils.general import Direction


class RecordingBackend:
    def __init__(self, existing=None):
        self.commands = []
        self.existing = existing

    def get_process_template(self, *args, **kwargs):
        return self.existing

    def get_resource_template(self, *args, **kwargs):
        return type("Template", (), {"id": uuid4()})()

    def find_resources_by_identity(self, *args, **kwargs):
        return []

    def execute(self, command, context):
        self.commands.append((command, context))
        return self.existing


def test_process_template_body_has_no_backend_mutation():
    backend = RecordingBackend()
    context = object()
    builder = ProcessTemplateBuilder(
        backend=backend,
        namespace_id=uuid4(),
        namespace_path="beamline/amx",
        name="draft-template",
        version="1.0",
        command_context=context,
    )

    builder.add_resource_slot("input", "container", Direction.input)
    builder.add_step("measure")

    assert backend.commands == []


def test_process_run_exception_discards_draft():
    backend = RecordingBackend()
    builder = ProcessRunBuilder(
        name="run",
        description="desc",
        template_name="template",
        version="1.0",
        namespace_id=uuid4(),
        backend=backend,
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
    backend = RecordingBackend()
    context = object()
    with ProcessTemplateBuilder(
        backend=backend,
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
    backend = RecordingBackend()
    builder = ProcessRunBuilder(
        name="run",
        description="desc",
        template_name="template",
        version="1.0",
        namespace_id=uuid4(),
        backend=backend,
        namespace_path="beamline/amx",
        template_id=uuid4(),
        command_context=object(),
    )

    builder.assign_resource("input", type("Resource", (), {"id": uuid4()})())
    builder.save()

    assert len(backend.commands) == 1
    assert isinstance(backend.commands[0][0], CreateProcessRun)


def test_resource_builder_has_no_construction_side_effect_and_submits_once():
    backend = RecordingBackend()
    builder = ResourceBuilder(
        name="resource",
        template_name="template",
        backend=backend,
        namespace_id=uuid4(),
        namespace_path="beamline/amx",
        command_context=object(),
    )

    assert backend.commands == []
    builder.save()

    assert len(backend.commands) == 1
    assert isinstance(backend.commands[0][0], CreateResource)
