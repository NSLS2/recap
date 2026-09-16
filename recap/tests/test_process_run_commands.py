def test_process_run_command_models_are_available():
    from recap.commands.models import CreateProcessRun, UpdateProcessRun

    assert CreateProcessRun and UpdateProcessRun


def test_process_run_builder_command_mode_submits_namespace_owned_command():
    from uuid import uuid4

    from recap.client.backend import ClientBackend
    from recap.dsl.process_builder import ProcessRunBuilder
    from recap.schemas.namespace import NamespaceContext

    class Backend:
        def query(self, schema, *args, **kwargs):
            if schema.__name__ == "ProcessTemplateSchema":
                return [type("Template", (), {"id": template_id})()]
            return []

        def execute(self, command, context):
            self.command = command
            return type("Run", (), {"id": uuid4(), "revision": 1})()

        def count(self, *args, **kwargs):
            return 0

        def list_child_namespaces(self, parent_path):
            return []

        def create_namespace(self, path, metadata, context):
            return None

        def update_namespace(
            self,
            namespace_id,
            expected_revision,
            metadata,
            status,
            context,
            *,
            etag=None,
        ):
            return None

        def get_namespace_context(self, path):
            return NamespaceContext(id=uuid4(), path=path)

    template_id = uuid4()
    adapter = Backend()
    backend = ClientBackend(
        reader=adapter,
        writer=adapter,
        namespaces=adapter,
        namespace_writer=adapter,
        context_resolver=adapter,
    )
    builder = ProcessRunBuilder(
        "run",
        "description",
        None,
        uuid4(),
        template_id=template_id,
        backend=backend,
        namespace_context=NamespaceContext(id=uuid4(), path="beamline/amx"),
        command_context=object(),
    )
    with builder:
        pass
    assert adapter.command.draft.name == "run"
