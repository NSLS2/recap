from unittest.mock import MagicMock

import pytest


def make_mock_info(backend):
    info = MagicMock()
    info.context = {"backend": backend}
    return info


def make_backend(tmp_path):
    from recap.client import RecapClient

    client = RecapClient.from_sqlite(str(tmp_path / "test.db"))
    client.create_namespace("test")
    return client.backend


def test_resolve_resources_returns_list(tmp_path):
    from recap.server.resolvers import resolve_resources

    backend = make_backend(tmp_path)
    info = make_mock_info(backend)
    result = resolve_resources(info, namespace_path="test", limit=10, offset=0)
    assert isinstance(result, list)


def test_resolve_namespaces_returns_list(tmp_path):
    from recap.server.resolvers import resolve_namespaces

    backend = make_backend(tmp_path)
    info = make_mock_info(backend)
    result = resolve_namespaces(info, namespace_path="test", limit=10, offset=0)
    assert isinstance(result, list)


def test_resolve_resources_count(tmp_path):
    from recap.server.resolvers import resolve_resources_count

    backend = make_backend(tmp_path)
    info = make_mock_info(backend)
    count = resolve_resources_count(info, namespace_path="test")
    assert count == 0


def test_resolve_resources_enforces_max_limit(tmp_path):
    import strawberry

    from recap.server.resolvers import resolve_resources

    backend = make_backend(tmp_path)
    info = make_mock_info(backend)
    with pytest.raises(strawberry.exceptions.StrawberryGraphQLError):
        resolve_resources(info, namespace_path="test", limit=99999, offset=0)


def test_resolve_process_runs_returns_list(tmp_path):
    from recap.server.resolvers import resolve_process_runs

    backend = make_backend(tmp_path)
    info = make_mock_info(backend)
    result = resolve_process_runs(info, namespace_path="test", limit=10, offset=0)
    assert isinstance(result, list)


def test_resolve_process_runs_count(tmp_path):
    from recap.server.resolvers import resolve_process_runs_count

    backend = make_backend(tmp_path)
    info = make_mock_info(backend)
    count = resolve_process_runs_count(info, namespace_path="test")
    assert count == 0


def test_resolve_process_templates_returns_list(tmp_path):
    from recap.server.resolvers import resolve_process_templates

    backend = make_backend(tmp_path)
    info = make_mock_info(backend)
    result = resolve_process_templates(info, namespace_path="test", limit=10, offset=0)
    assert isinstance(result, list)


def test_resolve_resource_templates_returns_list(tmp_path):
    from recap.server.resolvers import resolve_resource_templates

    backend = make_backend(tmp_path)
    info = make_mock_info(backend)
    result = resolve_resource_templates(info, namespace_path="test", limit=10, offset=0)
    assert isinstance(result, list)


def test_resolve_namespaces_count(tmp_path):
    from recap.server.resolvers import resolve_namespaces_count

    backend = make_backend(tmp_path)
    info = make_mock_info(backend)
    count = resolve_namespaces_count(info, namespace_path="test")
    assert count == 2
