def test_process_run_command_models_are_available():
    from recap.commands.models import CreateProcessRun, UpdateProcessRun

    assert CreateProcessRun and UpdateProcessRun


def test_process_run_builder_command_mode_submits_namespace_owned_command():
    from uuid import uuid4

    from recap.dsl.process_builder import ProcessRunBuilder

    class Backend:
        def execute(self, command, context):
            self.command = command
            return type("Run", (), {"id": uuid4(), "revision": 1})()

    backend = Backend()
    builder = ProcessRunBuilder(
        "run",
        "description",
        None,
        uuid4(),
        backend,
        template_id=uuid4(),
        namespace_path="beamline/amx",
        command_context=object(),
    )
    builder.save()
    assert backend.command.draft.name == "run"
