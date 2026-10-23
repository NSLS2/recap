"""Decode flat command responses for public result assertions."""

from recap.adapter.schema_registry import schema_for
from recap.adapter.transport import FlatQueryResult, decode_query_batch, serialize_model
from recap.client.identity import IdentityMap
from recap.client.materialization import BatchMaterializer


def command_data(response):
    payload = response.json()
    assert "entities" in payload and "roots" in payload and "adjacency" in payload
    envelope = FlatQueryResult.model_validate(payload)
    schema = schema_for(envelope.entity, envelope.projection)
    batch = decode_query_batch(schema, envelope)
    return serialize_model(BatchMaterializer(IdentityMap()).materialize(schema, batch)[0])
