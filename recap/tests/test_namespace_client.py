from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest


class FakeClient:
    def __init__(self):
        self.calls = []

    def query_maker(self, *, namespace, on_unloaded):
        self.calls.append(("query", namespace, on_unloaded))
        return self.calls[-1]

    def build_resource_template(self, **kwargs):
        self.calls.append(("resource-template", kwargs))
        return self.calls[-1]

    def copy_resource(self, **kwargs):
        self.calls.append(("copy", kwargs))
        return self.calls[-1]


def test_namespace_client_is_immutable_and_binds_query_context():
    from recap.client.namespace_client import NamespaceClient

    client = FakeClient()
    scoped = NamespaceClient(client, "beamline/amx")
    assert scoped.query_maker(on_unloaded="raise") == ("query", "beamline/amx", "raise")
    with pytest.raises(FrozenInstanceError):
        scoped.path = "beamline/fmx"


def test_namespace_client_builders_and_copy_use_scoped_path_without_leakage():
    from recap.client.namespace_client import NamespaceClient

    client = FakeClient()
    amx = NamespaceClient(client, "beamline/amx")
    fmx = NamespaceClient(client, "beamline/fmx")
    amx.build_resource_template(name="plate", type_names=["plate"])
    amx.copy_resource(source_resource_id=UUID(int=1), changes={"name": "clone"})
    fmx.copy_resource(source_resource_id=UUID(int=2))
    assert client.calls[0][1]["namespace_path"] == "beamline/amx"
    assert client.calls[1][1]["destination_namespace_path"] == "beamline/amx"
    assert client.calls[2][1]["destination_namespace_path"] == "beamline/fmx"


def test_namespace_creation_is_explicit():
    from recap.client.namespace_client import NamespaceClient

    assert hasattr(NamespaceClient, "create")
