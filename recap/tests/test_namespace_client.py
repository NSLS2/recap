from dataclasses import FrozenInstanceError
from typing import Any
from uuid import UUID

import pytest

from recap.schemas.resource import ResourceCopyOptions


class FakeClient:
    def __init__(self):
        self.calls: list[tuple[str, Any]] = []

    def query_maker(self, *, namespace: str, on_unloaded: str):
        self.calls.append(("query", (namespace, on_unloaded)))
        return self.calls[-1]

    def build_resource_template(
        self,
        *,
        name: str | None = None,
        type_names: list[str] | None = None,
        version: str = "1.0",
        resource_template_id: UUID | None = None,
        on_existing: str = "warn",
        namespace_path: str | None = None,
    ):
        self.calls.append(
            (
                "resource-template",
                (
                    name,
                    type_names,
                    version,
                    resource_template_id,
                    on_existing,
                    namespace_path,
                ),
            )
        )
        return self.calls[-1]

    def build_process_template(
        self,
        name: str | None = None,
        version: str | None = None,
        *,
        process_template_id: UUID | None = None,
        on_existing: str = "warn",
        namespace_path: str | None = None,
    ):
        self.calls.append(
            (
                "process-template",
                (name, version, process_template_id, on_existing, namespace_path),
            )
        )
        return self.calls[-1]

    def build_resource(
        self,
        name: str | None = None,
        template_name: str | None = None,
        template_version: str = "1.0",
        *,
        resource_id: UUID | None = None,
        on_existing: str = "warn",
        parent=None,
        namespace_path: str | None = None,
    ):
        self.calls.append(
            (
                "resource",
                (
                    name,
                    template_name,
                    template_version,
                    resource_id,
                    on_existing,
                    parent,
                    namespace_path,
                ),
            )
        )
        return self.calls[-1]

    def build_process_run(
        self,
        name: str | None = None,
        description: str | None = None,
        template_name: str | None = None,
        version: str | None = None,
        *,
        process_run_id: UUID | None = None,
        on_existing: str = "warn",
        namespace_path: str | None = None,
    ):
        self.calls.append(
            (
                "process-run",
                (
                    name,
                    description,
                    template_name,
                    version,
                    process_run_id,
                    on_existing,
                    namespace_path,
                ),
            )
        )
        return self.calls[-1]

    def copy_resource(
        self,
        source_resource_id: UUID,
        destination_namespace_id: UUID | None = None,
        options: ResourceCopyOptions | None = None,
        *,
        destination_namespace_path: str | None = None,
    ):
        self.calls.append(
            (
                "copy",
                (
                    source_resource_id,
                    destination_namespace_id,
                    options,
                    destination_namespace_path,
                ),
            )
        )
        return self.calls[-1]


def test_namespace_client_is_immutable_and_binds_query_context():
    from recap.client.namespace_client import NamespaceClient

    client = FakeClient()
    scoped = NamespaceClient(client, "beamline/amx")
    assert scoped.query_maker(on_unloaded="raise") == (
        "query",
        ("beamline/amx", "raise"),
    )
    with pytest.raises(FrozenInstanceError):
        scoped.path = "beamline/fmx"


def test_namespace_client_passes_explicit_builder_paths_without_state_leakage():
    from recap.client.namespace_client import NamespaceClient

    client = FakeClient()
    amx = NamespaceClient(client, "beamline/amx")
    fmx = NamespaceClient(client, "beamline/fmx")
    amx.build_resource_template(name="plate", type_names=["plate"])
    amx.build_process_template("collect", "1.0")
    amx.build_resource("plate-1", "plate")
    amx.build_process_run("run-1", "initial", "collect", "1.0")
    fmx.build_resource_template(name="plate", type_names=["plate"])

    assert [call[1][-1] for call in client.calls] == [
        "beamline/amx",
        "beamline/amx",
        "beamline/amx",
        "beamline/amx",
        "beamline/fmx",
    ]


def test_namespace_client_copy_preserves_source_uuid_and_destination_path():
    from recap.client.namespace_client import NamespaceClient

    source = UUID(int=1)
    client = FakeClient()
    NamespaceClient(client, "beamline/amx").copy_resource(
        source_resource_id=source, changes={"name": "clone"}
    )

    assert client.calls == [
        (
            "copy",
            (
                source,
                None,
                ResourceCopyOptions(name="clone"),
                "beamline/amx",
            ),
        )
    ]


def test_namespace_creation_is_explicit():
    from recap.client.namespace_client import NamespaceClient

    assert hasattr(NamespaceClient, "create")
