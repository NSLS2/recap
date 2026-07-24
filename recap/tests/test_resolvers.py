from unittest.mock import MagicMock

import pytest


def make_mock_info(backend):
    info = MagicMock()
    info.context = {"backend": backend}
    return info


def make_backend(tmp_path):
    from recap.client import RecapClient

    client = RecapClient.from_sqlite(str(tmp_path / "test.db"))
    return client.backend


def test_resolve_resources_returns_list(tmp_path):
    from recap.server.resolvers import resolve_resources

    backend = make_backend(tmp_path)
    info = make_mock_info(backend)
    result = resolve_resources(info, campaign_id=None, limit=10, offset=0)
    assert isinstance(result, list)


def test_resolve_campaigns_returns_list(tmp_path):
    from recap.server.resolvers import resolve_campaigns

    backend = make_backend(tmp_path)
    info = make_mock_info(backend)
    result = resolve_campaigns(info, limit=10, offset=0)
    assert isinstance(result, list)


def test_resolve_resources_count(tmp_path):
    from recap.server.resolvers import resolve_resources_count

    backend = make_backend(tmp_path)
    info = make_mock_info(backend)
    count = resolve_resources_count(info, campaign_id=None)
    assert count == 0


def test_resolve_resources_enforces_max_limit(tmp_path):
    import strawberry

    from recap.server.resolvers import resolve_resources

    backend = make_backend(tmp_path)
    info = make_mock_info(backend)
    with pytest.raises(strawberry.exceptions.StrawberryGraphQLError):
        resolve_resources(info, campaign_id=None, limit=99999, offset=0)


def test_resolve_process_runs_returns_list(tmp_path):
    from recap.server.resolvers import resolve_process_runs

    backend = make_backend(tmp_path)
    info = make_mock_info(backend)
    result = resolve_process_runs(info, campaign_id=None, limit=10, offset=0)
    assert isinstance(result, list)


def test_resolve_process_runs_count(tmp_path):
    from recap.server.resolvers import resolve_process_runs_count

    backend = make_backend(tmp_path)
    info = make_mock_info(backend)
    count = resolve_process_runs_count(info, campaign_id=None)
    assert count == 0


def test_resolve_process_templates_returns_list(tmp_path):
    from recap.server.resolvers import resolve_process_templates

    backend = make_backend(tmp_path)
    info = make_mock_info(backend)
    result = resolve_process_templates(info, limit=10, offset=0)
    assert isinstance(result, list)


def test_resolve_resource_templates_returns_list(tmp_path):
    from recap.server.resolvers import resolve_resource_templates

    backend = make_backend(tmp_path)
    info = make_mock_info(backend)
    result = resolve_resource_templates(info, limit=10, offset=0)
    assert isinstance(result, list)


def test_resolve_campaigns_count(tmp_path):
    from recap.server.resolvers import resolve_campaigns_count

    backend = make_backend(tmp_path)
    info = make_mock_info(backend)
    count = resolve_campaigns_count(info)
    assert count == 0
