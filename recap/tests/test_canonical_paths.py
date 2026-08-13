from inspect import signature
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


@pytest.mark.parametrize("attribute", ["begin", "session"])
def test_local_backend_does_not_expose_legacy_transaction_api(attribute):
    assert not hasattr(LocalBackend, attribute)


def test_request_dependency_does_not_begin_transaction():
    backend = next(get_local_backend(SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_factory=object())))))
    assert isinstance(backend, LocalBackend)
    assert not hasattr(backend, "_session")


def test_legacy_namespace_api_is_removed():
    from recap.client.base_client import RecapClient

    assert not hasattr(RecapClient, "set_namespace")
    assert "path" in signature(RecapClient.namespace).parameters
