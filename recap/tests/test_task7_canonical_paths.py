import inspect
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from recap.adapter.local import LocalBackend
from recap.dsl.process_builder import ProcessRunBuilder, ProcessTemplateBuilder
from recap.dsl.resource_builder import ResourceBuilder, ResourceTemplateBuilder
from recap.server.dependencies import get_local_backend


def test_builders_reject_direct_construction_without_command_context():
    backend = SimpleNamespace()
    namespace_id = uuid4()

    with pytest.raises(ValueError, match="command"):
        ProcessTemplateBuilder(backend, namespace_id, "process", "1.0")
    with pytest.raises(ValueError, match="command"):
        ProcessRunBuilder("run", "description", "process", namespace_id, backend)
    with pytest.raises(ValueError, match="command"):
        ResourceTemplateBuilder("resource", ["sample"], backend=backend, namespace_id=namespace_id)
    with pytest.raises(ValueError, match="command"):
        ResourceBuilder("resource", "template", backend=backend, namespace_id=namespace_id)


def test_local_backend_has_no_persistent_uow_surface():
    assert not hasattr(LocalBackend, "begin")
    assert not hasattr(LocalBackend, "session")
    assert not hasattr(LocalBackend, "_get_session")


def test_local_backend_mutations_are_command_only():
    source = inspect.getsource(LocalBackend)
    assert "CommandService" in source
    assert "self.backend.begin()" not in source
    assert "def session" not in source


def test_request_dependency_does_not_begin_transaction():
    backend = next(get_local_backend(SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_factory=object())))))
    assert isinstance(backend, LocalBackend)
    assert not hasattr(backend, "_session")


def test_legacy_namespace_api_is_removed():
    from recap.client.base_client import RecapClient

    assert not hasattr(RecapClient, "set_namespace")
    assert "path" in inspect.signature(RecapClient.namespace).parameters


def test_task7_production_paths_contain_no_legacy_symbols():
    root = Path(__file__).parents[1]
    paths = [root / "adapter", root / "client", root / "dsl", root / "server"]
    legacy = (
        "SQL" + "UnitOfWork",
        "_ensure" + "_uow",
        "_uow",
        "_restart_uow",
        "self.session",
        "set_" + "namespace(",
        "LocalBackend." + "begin()",
    )
    matches = [
        path
        for directory in paths
        for path in directory.rglob("*.py")
        if any(token in path.read_text() for token in legacy)
    ]
    assert matches == []


def test_builders_have_no_legacy_mutation_entry_points():
    source = "\n".join(
        (
            inspect.getsource(ProcessTemplateBuilder),
            inspect.getsource(ProcessRunBuilder),
            inspect.getsource(ResourceTemplateBuilder),
            inspect.getsource(ResourceBuilder),
        )
    )
    assert "set_process_template_status" not in source
    assert "set_process_run_status" not in source
    assert "set_resource_template_status" not in source
    assert "set_resource_status" not in source
    assert "def persist" not in source
    assert "def _ensure_uow" not in source
    assert "def _restart_uow" not in source
    assert "self.session" not in source
    assert "create_process_template(" not in inspect.getsource(ProcessTemplateBuilder)
    assert "_create_or_reuse_resource" not in inspect.getsource(ResourceBuilder)
    assert "hasattr(self.backend, \"query\")" not in inspect.getsource(ResourceBuilder)
    for method in (
        "add_resource_slot",
        "add_step",
        "bind_slot",
        "add_attr_group",
        "add_attribute",
        "create_resource",
        "copy_resource",
        "create_process_run",
        "assign_resource",
        "set_params",
        "add_child_step",
    ):
        assert f"def {method}" not in inspect.getsource(LocalBackend)
