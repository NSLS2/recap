import pytest
from unittest.mock import patch, MagicMock
from recap.dsl.query import QuerySpec
from recap.schemas.resource import ResourceSchema
from recap.schemas.process import CampaignSchema


def test_graphql_adapter_importable():
    from recap.adapter.graphql import GraphQLAdapter

    assert GraphQLAdapter is not None


def test_graphql_adapter_implements_read_backend():
    from recap.adapter.graphql import GraphQLAdapter
    from recap.adapter import ReadBackend

    assert issubclass(GraphQLAdapter, ReadBackend) or hasattr(GraphQLAdapter, "query")


def test_graphql_adapter_context_manager():
    from recap.adapter.graphql import GraphQLAdapter

    with GraphQLAdapter("http://localhost:9999/graphql") as adapter:
        assert adapter is not None


def test_query_spec_translator_resource_root_field():
    from recap.adapter.graphql import QuerySpecTranslator

    spec = QuerySpec()
    t = QuerySpecTranslator(ResourceSchema, spec)
    assert t.root_field_name() == "resources"


def test_query_spec_translator_campaign_root_field():
    from recap.adapter.graphql import QuerySpecTranslator

    spec = QuerySpec()
    t = QuerySpecTranslator(CampaignSchema, spec)
    assert t.root_field_name() == "campaigns"


def test_query_spec_translator_generates_query_string():
    from recap.adapter.graphql import QuerySpecTranslator

    spec = QuerySpec(limit=10, offset=0)
    t = QuerySpecTranslator(ResourceSchema, spec)
    q = t.to_graphql()
    assert "resources" in q
    assert "limit" in q
    assert "10" in q


def test_graphql_adapter_query_calls_post(tmp_path):
    from recap.adapter.graphql import GraphQLAdapter

    mock_response = MagicMock()
    mock_response.json.return_value = {"data": {"resources": []}}
    mock_response.raise_for_status = MagicMock()

    with patch("httpx2.Client.post", return_value=mock_response):
        adapter = GraphQLAdapter("http://localhost:9999/graphql")
        spec = QuerySpec(limit=10)
        results = adapter.query(ResourceSchema, spec)
        assert results == []
        adapter.close()


def test_graphql_adapter_count_calls_post(tmp_path):
    from recap.adapter.graphql import GraphQLAdapter

    mock_response = MagicMock()
    mock_response.json.return_value = {"data": {"resources_count": 42}}
    mock_response.raise_for_status = MagicMock()

    with patch("httpx2.Client.post", return_value=mock_response):
        adapter = GraphQLAdapter("http://localhost:9999/graphql")
        spec = QuerySpec()
        count = adapter.count(ResourceSchema, spec)
        assert count == 42
        adapter.close()
