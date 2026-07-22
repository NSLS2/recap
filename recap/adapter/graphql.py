"""GraphQL read-only adapter for RecapClient.

Implements ReadBackend by translating QuerySpec → GraphQL query strings
and posting to a recap GraphQL server endpoint.
"""
from __future__ import annotations

from uuid import UUID

import httpx2 as httpx

from recap.dsl.query import QuerySpec, SchemaT
from recap.schemas.process import (
    CampaignSchema,
    ProcessRunSchema,
    ProcessTemplateSchema,
)
from recap.schemas.resource import (
    ResourceSchema,
    ResourceTemplateSchema,
)
from recap.schemas.step import StepSchema

# Maps Pydantic schema type → (list field name, count field name)
_SCHEMA_FIELD_MAP: dict[type, tuple[str, str]] = {
    ResourceSchema: ("resources", "resources_count"),
    ResourceTemplateSchema: ("resource_templates", "resource_templates_count"),
    ProcessRunSchema: ("process_runs", "process_runs_count"),
    ProcessTemplateSchema: ("process_templates", "process_templates_count"),
    CampaignSchema: ("campaigns", "campaigns_count"),
}

# Minimal field selections per schema type
_SCHEMA_FIELDS: dict[type, str] = {
    ResourceSchema: "id name create_date modified_date",
    ResourceTemplateSchema: "id name version create_date modified_date",
    ProcessRunSchema: "id name description create_date modified_date",
    ProcessTemplateSchema: "id name version create_date modified_date",
    CampaignSchema: "id name proposal create_date modified_date",
}


class QuerySpecTranslator:
    """Translates a QuerySpec + schema type into a GraphQL query string."""

    def __init__(self, schema: type, spec: QuerySpec):
        self._schema = schema
        self._spec = spec

    def root_field_name(self) -> str:
        return _SCHEMA_FIELD_MAP[self._schema][0]

    def count_field_name(self) -> str:
        return _SCHEMA_FIELD_MAP[self._schema][1]

    def _build_args(self) -> str:
        parts: list[str] = []
        spec = self._spec
        if spec.campaign_id is not None:
            parts.append(f'campaignId: "{spec.campaign_id}"')
        if spec.limit is not None:
            parts.append(f"limit: {spec.limit}")
        if spec.offset is not None:
            parts.append(f"offset: {spec.offset}")
        return f"({', '.join(parts)})" if parts else ""

    def _build_count_args(self) -> str:
        parts: list[str] = []
        spec = self._spec
        if spec.campaign_id is not None:
            parts.append(f'campaignId: "{spec.campaign_id}"')
        return f"({', '.join(parts)})" if parts else ""

    def to_graphql(self) -> str:
        field = self.root_field_name()
        args = self._build_args()
        fields = _SCHEMA_FIELDS.get(self._schema, "id name create_date modified_date")
        return f"{{ {field}{args} {{ {fields} }} }}"

    def to_graphql_count(self) -> str:
        field = self.count_field_name()
        args = self._build_count_args()
        return f"{{ {field}{args} }}"


class GraphQLAdapter:
    """ReadBackend implementation over HTTP GraphQL.

    Translates QuerySpec → GraphQL query string via QuerySpecTranslator,
    POSTs to the server, and deserializes JSON → Pydantic schemas.

    Phase 1 constraint: read-only. Write methods raise NotImplementedError.
    Use LocalBackend (via RecapClient.from_url()) for writes.
    """

    def __init__(self, graphql_url: str):
        self._url = graphql_url
        self._client = httpx.Client(timeout=30.0)

    def close(self) -> None:
        """Close the underlying HTTP client and release connections."""
        self._client.close()

    def __enter__(self) -> "GraphQLAdapter":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def query(self, schema: type[SchemaT], spec: QuerySpec) -> list[SchemaT]:
        translator = QuerySpecTranslator(schema, spec)
        gql = translator.to_graphql()
        response = self._client.post(self._url, json={"query": gql})
        response.raise_for_status()
        body = response.json()
        items = body["data"][translator.root_field_name()]
        return [schema.model_validate(item) for item in items]

    def count(self, schema: type[SchemaT], spec: QuerySpec) -> int:
        translator = QuerySpecTranslator(schema, spec)
        gql = translator.to_graphql_count()
        response = self._client.post(self._url, json={"query": gql})
        response.raise_for_status()
        return response.json()["data"][translator.count_field_name()]

    # ------------------------------------------------------------------ #
    # Read methods delegated to server (minimal implementations for now)
    # Full implementations to be added as needed in follow-up tasks.
    # ------------------------------------------------------------------ #

    def get_resource(
        self,
        name: str,
        template_name: str,
        template_version: str | None = "1.0",
        expand: bool = False,
    ) -> ResourceSchema:
        raise NotImplementedError("get_resource via GraphQL not yet implemented — use query()")

    def get_resource_template(
        self,
        name: str | None,
        version: str | None = None,
        id: UUID | str | None = None,
        parent=None,
        expand=False,
    ):
        raise NotImplementedError("get_resource_template via GraphQL not yet implemented — use query()")

    def get_process_template(
        self,
        name: str | None,
        version: str | None,
        expand=False,
        id: UUID | str | None = None,
    ):
        raise NotImplementedError("get_process_template via GraphQL not yet implemented — use query()")

    def find_resources_by_identity(
        self, name: str, parent_id: UUID | None, resource_template_id: UUID
    ) -> list:
        raise NotImplementedError("find_resources_by_identity via GraphQL not yet implemented")

    def get_steps(self, process_run: ProcessRunSchema) -> list[StepSchema]:
        raise NotImplementedError("get_steps via GraphQL not yet implemented")

    def get_params(self, step_schema: StepSchema):
        raise NotImplementedError("get_params via GraphQL not yet implemented")
