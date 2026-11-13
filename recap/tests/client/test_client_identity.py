import gc
import weakref
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import RLock
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from recap.client.backend import ClientBackend
from recap.client.base_client import RecapClient
from recap.client.connection_state import ConnectionState
from recap.client.identity import IdentityMap
from recap.schemas.namespace import NamespaceContext, NamespaceSchema
from recap.schemas.resource import ResourceSchema


def _resource(resource_id):
    return ResourceSchema.model_construct(
        id=resource_id,
        create_date=datetime(2026, 1, 1),
        modified_date=datetime(2026, 1, 1),
        namespace_id=uuid4(),
        status="ACTIVE",
        revision=1,
        name="sample",
    )


def _backend(*, identities=None, capabilities=None):
    backend = ClientBackend.__new__(ClientBackend)
    object.__setattr__(backend, "_close_lock", RLock())
    object.__setattr__(backend, "_closed", False)
    object.__setattr__(backend, "identity_map", identities or IdentityMap())
    for name, capability in zip(
        ("reader", "writer", "namespaces", "namespace_writer"),
        capabilities or [object() for _ in range(4)],
        strict=True,
    ):
        object.__setattr__(backend, name, capability)
    return backend


def test_registration_is_storage_only_and_requires_canonical_instance():
    identities = IdentityMap()
    entity_id = uuid4()
    first = _resource(entity_id)
    second = first.model_copy(update={"name": "different"})
    assert identities.register(("resource", entity_id), first) is first
    assert identities.register(("resource", entity_id), first) is first
    with pytest.raises(ValueError, match="already registered"):
        identities.register(("resource", entity_id), second)
    assert first.name == "sample"
    assert identities.get(("resource", entity_id)) is first


def test_registration_and_clear_share_reentrant_materialization_lock():
    identities = IdentityMap()
    entity_id = uuid4()
    with identities.locked():
        model = _resource(entity_id)
        identities.register(("resource", entity_id), model)
        assert identities.get(("resource", entity_id)) is model
        identities.clear()
        assert identities.get(("resource", entity_id)) is None


def test_identity_map_retains_model_until_clear():
    identities = IdentityMap()
    model = _resource(uuid4())
    reference = weakref.ref(model)
    identities.register(("resource", model.id), model)
    del model
    gc.collect()
    assert reference() is not None
    identities.clear()
    gc.collect()
    assert reference() is None


def test_entity_family_and_uuid_separate_keys():
    identities = IdentityMap()
    entity_id = uuid4()
    resource = _resource(entity_id)
    namespace = NamespaceSchema.model_construct(id=entity_id, path="beamline")
    identities.register(("resource", entity_id), resource)
    identities.register(("namespace", entity_id), namespace)
    assert identities.get(("resource", entity_id)) is resource
    assert identities.get(("namespace", entity_id)) is namespace
    assert identities.get(("resource", uuid4())) is None


@pytest.mark.parametrize("kind", ["namespace", "property", "unknown"])
def test_registration_rejects_wrong_family_and_noncanonical_models(kind):
    identities = IdentityMap()
    model = _resource(uuid4())
    with pytest.raises(ValueError, match="matching entity"):
        identities.register((kind, model.id), model)
    assert identities.get((kind, model.id)) is None


def test_context_and_wrong_id_cannot_enter_canonical_storage():
    identities = IdentityMap()
    context = NamespaceContext(id=uuid4(), path="beamline")
    with pytest.raises(ValueError):
        identities.register(("namespace", context.id), context)
    with pytest.raises(ValueError):
        identities.register(("resource", uuid4()), _resource(uuid4()))


def test_concurrent_registration_keeps_one_canonical_instance():
    identities = IdentityMap()
    entity_id = uuid4()

    def register(_index):
        with identities.locked():
            current = identities.get(("resource", entity_id))
            return current or identities.register(
                ("resource", entity_id), _resource(entity_id)
            )

    with ThreadPoolExecutor(max_workers=8) as executor:
        models = list(executor.map(register, range(32)))
    assert all(model is models[0] for model in models)


def test_storage_does_not_traverse_self_referential_graph():
    identities = IdentityMap()
    model = _resource(uuid4())
    model.__dict__["children"] = {"self": model}
    assert identities.register(("resource", model.id), model) is model
    assert model.children["self"] is model


def test_connection_state_clears_identity_map_once_on_final_close():
    backend = _backend()
    state = ConnectionState(backend=backend)
    state.acquire()
    state.acquire()
    model = _resource(uuid4())
    backend.identity_map.register(("resource", model.id), model)
    state.release()
    assert backend.identity_map.get(("resource", model.id)) is model
    state.release()
    state.close()
    assert backend.identity_map.get(("resource", model.id)) is None


def test_direct_backend_close_clears_identity_map_idempotently():
    backend = _backend()
    model = _resource(uuid4())
    backend.identity_map.register(("resource", model.id), model)
    backend.close()
    backend.close()
    assert backend.identity_map.get(("resource", model.id)) is None


def test_concurrent_direct_backend_close_closes_capabilities_and_identity_once():
    class Closable:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    capabilities = [Closable() for _ in range(4)]
    backend = _backend(capabilities=capabilities)
    model = _resource(uuid4())
    backend.identity_map.register(("resource", model.id), model)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _index: backend.close(), range(32)))
    assert backend.identity_map.get(("resource", model.id)) is None
    assert [capability.close_calls for capability in capabilities] == [1] * 4


def test_direct_backend_close_closes_optional_capabilities_once_by_identity():
    class Closable:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    capabilities = [Closable() for _ in range(6)]
    backend = _backend(capabilities=capabilities[:4])
    object.__setattr__(backend, "context_resolver", capabilities[4])
    object.__setattr__(backend, "permissions", capabilities[5])
    backend.close()
    backend.close()
    assert [capability.close_calls for capability in capabilities] == [1] * 6


def test_backend_close_retries_failed_capability_without_repeating_successful_cleanup():
    class FlakyIdentityMap(IdentityMap):
        def __init__(self):
            super().__init__()
            self.clear_calls = 0

        def clear(self):
            self.clear_calls += 1
            if self.clear_calls == 1:
                raise RuntimeError("clear failed")
            super().clear()

    class Closable:
        def __init__(self, failures=0):
            self.close_calls = 0
            self.failures = failures

        def close(self):
            self.close_calls += 1
            if self.failures:
                self.failures -= 1
                raise RuntimeError("close failed")

    identities = FlakyIdentityMap()
    failing, successful = Closable(2), Closable()
    backend = _backend(
        identities=identities,
        capabilities=[failing, successful, successful, successful],
    )
    model = _resource(uuid4())
    identities.register(("resource", model.id), model)
    with pytest.raises(RuntimeError, match="clear failed"):
        backend.close()
    assert identities.get(("resource", model.id)) is model
    assert failing.close_calls == successful.close_calls == 1
    with pytest.raises(RuntimeError, match="close failed"):
        backend.close()
    assert identities.get(("resource", model.id)) is None
    assert identities.clear_calls == 2
    assert failing.close_calls == 2
    assert successful.close_calls == 1
    backend.close()
    backend.close()
    assert failing.close_calls == 3
    assert identities.clear_calls == 2


def test_recap_client_close_retries_failed_shared_cleanup(monkeypatch):
    class FlakyIdentityMap(IdentityMap):
        def __init__(self):
            super().__init__()
            self.clear_calls = 0

        def clear(self):
            self.clear_calls += 1
            if self.clear_calls == 1:
                raise RuntimeError("clear failed")
            super().clear()

    class Resolver:
        def get_namespace_context(self, path):
            return NamespaceContext(id=UUID(int=0), path=path)

    backend = _backend(identities=FlakyIdentityMap())
    object.__setattr__(backend, "context_resolver", Resolver())
    client = RecapClient._from_backends(backend, namespace="")
    state = client.connection_state
    with pytest.raises(RuntimeError, match="clear failed"):
        client.close()
    assert not client._closed
    assert not state.closed
    client.close()
    assert client._closed
    assert state.closed
    assert backend.identity_map.clear_calls == 2


def test_root_and_scoped_clients_share_identity_map(tmp_path):
    root = RecapClient.from_sqlite(tmp_path / "identity.db")
    root.create_namespace("beamline")
    scoped = root.namespace("beamline")
    assert (
        scoped.connection_state.backend.identity_map
        is root.connection_state.backend.identity_map
    )
    scoped.close()
    root.close()


def test_final_root_close_clears_shared_identity_map_once(tmp_path):
    root = RecapClient.from_sqlite(tmp_path / "identity.db")
    root.create_namespace("beamline")
    scoped = root.namespace("beamline")
    with patch.object(
        root.connection_state.backend.identity_map,
        "clear",
        wraps=root.connection_state.backend.identity_map.clear,
    ) as clear:
        scoped.close()
        root.close()
    clear.assert_called_once_with()


def test_separate_backend_connections_have_separate_identity_maps():
    first, second = RecapClient.from_sqlite(), RecapClient.from_sqlite()
    assert (
        first.connection_state.backend.identity_map
        is not second.connection_state.backend.identity_map
    )
    first.close()
    second.close()
