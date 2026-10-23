"""Decode flat command responses for public result assertions."""

from recap.adapter.schema_registry import schema_for
from recap.adapter.transport import FlatQueryResult, decode_query_batch
from recap.client.identity import IdentityMap
from recap.client.materialization import BatchMaterializer


def command_data(response):
    payload = response.json()
    assert "entities" in payload and "roots" in payload and "adjacency" in payload
    assert len(payload["roots"]) == 1
    root = payload["roots"][0]
    record = next(
        record
        for record in payload["entities"]
        if record["kind"] == root["kind"] and record["id"] == root["id"]
    )
    return record["fields"] | {"id": record["id"], "revision": record["revision"]}


def command_model(response):
    payload = response.json()
    envelope = FlatQueryResult.model_validate(payload)
    schema = schema_for(envelope.entity, envelope.projection)
    batch = decode_query_batch(schema, envelope)
    return BatchMaterializer(IdentityMap()).materialize(schema, batch)[0]
