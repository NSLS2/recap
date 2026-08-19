from datetime import datetime
from uuid import UUID, uuid4

import pytest

from recap.client.backend import ClientBackend
from recap.client.base_client import RecapClient
from recap.commands.context import build_local_command_context
from recap.commands.models import UpdateNamespace
from recap.lifecycle import LifecycleStatus
from recap.schemas.namespace import NamespaceContext, NamespaceSchema
from recap.schemas.resource import ResourceRef, ResourceSchema, ResourceTemplateRef


def _namespace(
    namespace_id: UUID,
    *,
    revision: int = 1,
    path: str = "beamline",
    status: LifecycleStatus = LifecycleStatus.ACTIVE,
) -> NamespaceSchema:
    return NamespaceSchema.model_construct(
        id=namespace_id,
        create_date=datetime(2026, 1, 1),
        modified_date=datetime(2026, 1, revision),
        path=path,
        parent_id=None,
        status=status,
        revision=revision,
        metadata={},
    )


def _resource(
    resource_id: UUID,
    *,
    child: ResourceSchema | ResourceRef | None = None,
    name: str = "root",
    namespace_id: UUID | None = None,
):
    template = ResourceTemplateRef.model_construct(
        id=uuid4(),
        create_date=datetime(2026, 1, 1),
        modified_date=datetime(2026, 1, 1),
        namespace_id=namespace_id or uuid4(),
        status=LifecycleStatus.ACTIVE,
        revision=1,
        name="Sample",
        version="1.0",
    )
    return ResourceSchema.model_construct(
        id=resource_id,
        create_date=datetime(2026, 1, 1),
        modified_date=datetime(2026, 1, 1),
        namespace_id=namespace_id or uuid4(),
        status=LifecycleStatus.ACTIVE,
        revision=1,
        name=name,
        copied_from_id=None,
        template=template,
        parent=None,
        children={} if child is None else {"child": child},
        properties={},
    )


class _FakeBackendBase:
    def __init__(self, result_factory, command_result_factory=None):
        self._result_factory = result_factory
        self._command_result_factory = command_result_factory
        self.query_calls = 0

    def count(self, schema, spec, *, namespace_path):
        return 1

    def execute(self, command, context, *, etag_override=None):
        return self._command_result_factory(command)

    def create_namespace(self, path, metadata, context):
        return NamespaceContext(id=uuid4(), path=path)

    def update_namespace(self, namespace_id, expected_revision, metadata, status, context, *, etag=None):
        return _namespace(namespace_id, revision=expected_revision + 1)

    def list_child_namespaces(self, parent_path):
        return []

    def get_namespace_context(self, path):
        return NamespaceContext(id=uuid4(), path=path)


class _LocalFakeBackend(_FakeBackendBase):
    """Local-shaped fake: adapter returns already-hydrated domain models."""

    def query(self, schema, spec, *, namespace_path):
        self.query_calls += 1
        return [self._result_factory(schema, self.query_calls, namespace_path)]


class _RESTFakeBackend(_FakeBackendBase):
    """REST-shaped fake: adapter returns models after a wire round trip."""

    def query(self, schema, spec, *, namespace_path):
        self.query_calls += 1
        result = self._result_factory(schema, self.query_calls, namespace_path)
        return [schema.model_validate(result.model_dump(mode="json"))]


def _client_backend(reader):
    return ClientBackend(
        reader=reader,
        writer=reader,
        namespaces=reader,
        namespace_writer=reader,
        context_resolver=reader,
    )


@pytest.fixture(params=[_LocalFakeBackend, _RESTFakeBackend], ids=["local", "rest"])
def fake_backend(request):
    """Exercise client boundary with distinct local and REST-shaped readers."""
    namespace_id = uuid4()

    def result_factory(schema, _call, _namespace_path):
        assert schema is NamespaceSchema
        return _namespace(namespace_id)

    return _client_backend(request.param(result_factory))


def test_repeated_local_and_rest_queries_return_same_canonical_object(fake_backend):
    first = fake_backend.query(NamespaceSchema, object(), namespace_path="beamline")[0]
    second = fake_backend.query(NamespaceSchema, object(), namespace_path="beamline")[0]

    assert first is second


def test_nested_resource_and_direct_resource_query_share_identity():
    parent_id = uuid4()
    child_id = uuid4()
    direct_child = ResourceRef.model_construct(
        id=child_id,
        create_date=datetime(2026, 1, 1),
        modified_date=datetime(2026, 1, 1),
        namespace_id=uuid4(),
        status=LifecycleStatus.ACTIVE,
        revision=1,
        name="child",
        template=ResourceTemplateRef.model_construct(
            id=uuid4(),
            create_date=datetime(2026, 1, 1),
            modified_date=datetime(2026, 1, 1),
            namespace_id=uuid4(),
            status=LifecycleStatus.ACTIVE,
            revision=1,
            name="Sample",
            version="1.0",
        ),
    )

    def result_factory(schema, call, _namespace_path):
        assert schema is ResourceSchema
        if call == 1:
            return _resource(parent_id, child=direct_child)
        return _resource(child_id, name="child", namespace_id=direct_child.namespace_id)

    backend = _client_backend(_LocalFakeBackend(result_factory))
    nested = backend.query(ResourceSchema, object(), namespace_path="beamline")[0].children["child"]
    direct = backend.query(ResourceSchema, object(), namespace_path="beamline")[0]

    assert nested is direct


def test_command_result_updates_object_from_earlier_query():
    namespace_id = uuid4()

    def result_factory(schema, _call, _namespace_path):
        assert schema is NamespaceSchema
        return _namespace(namespace_id, revision=1)

    def command_result(command):
        assert isinstance(command, UpdateNamespace)
        return _namespace(
            namespace_id,
            revision=2,
            path="beamline/updated",
            status=LifecycleStatus.ARCHIVED,
        )

    backend = _client_backend(_LocalFakeBackend(result_factory, command_result))
    held = backend.query(NamespaceSchema, object(), namespace_path="beamline")[0]

    result = backend._execute(
        UpdateNamespace(
            namespace_id=namespace_id,
            expected_revision=1,
            metadata={"owner": "science"},
            status=None,
        ),
        build_local_command_context(),
    )

    assert result is held
    assert held.revision == 2
    assert held.path == "beamline/updated"
    assert held.status is LifecycleStatus.ARCHIVED


def test_namespace_scoped_view_shares_root_identity():
    namespace_id = uuid4()

    def result_factory(schema, _call, namespace_path):
        assert schema is NamespaceSchema
        return _namespace(namespace_id)

    reader = _LocalFakeBackend(result_factory)
    backend = _client_backend(reader)
    root = RecapClient._from_backends(backend, namespace="beamline")
    scoped = root.namespace("amx")

    try:
        root_result = root.query_maker().namespaces().all()[0]
        scoped_result = scoped.query_maker().namespaces().all()[0]
    finally:
        scoped.close()
        root.close()

    assert root_result is scoped_result
