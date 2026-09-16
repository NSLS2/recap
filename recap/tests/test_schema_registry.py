import pytest
from pydantic import BaseModel

from recap.adapter.schema_registry import (
    SCHEMA_REGISTRY,
    SchemaRegistration,
    SchemaRegistry,
)
from recap.db.namespace import Namespace
from recap.db.process import ProcessRun, ProcessTemplate
from recap.db.resource import Resource, ResourceTemplate
from recap.schemas.namespace import NamespaceSchema
from recap.schemas.process import ProcessRunSchema, ProcessTemplateSchema
from recap.schemas.resource import ResourceSchema, ResourceTemplateSchema


def test_public_query_registrations_are_complete():
    expected = {
        "namespace": (NamespaceSchema, Namespace),
        "resource_template": (ResourceTemplateSchema, ResourceTemplate),
        "resource": (ResourceSchema, Resource),
        "process_template": (ProcessTemplateSchema, ProcessTemplate),
        "process_run": (ProcessRunSchema, ProcessRun),
    }

    SCHEMA_REGISTRY.validate_complete()

    assert set(SCHEMA_REGISTRY.keys()) == set(expected)
    for key, (model, orm_model) in expected.items():
        registration = SCHEMA_REGISTRY.by_key(key)
        assert registration.model is model
        assert registration.orm_model is orm_model
        assert registration.hydrator is not None
        if key != "namespace":
            assert registration.loader_capabilities
        assert SCHEMA_REGISTRY.by_model(model) is registration
        assert {"full", "ref"} <= set(registration.projections)


def test_registry_rejects_duplicate_keys_and_models():
    class First(BaseModel):
        value: str

    class Second(BaseModel):
        value: str

    registration = SchemaRegistration(
        key="first",
        model=First,
        orm_model=Namespace,
        hydrator=First.model_validate,
        loader_capabilities=(),
    )
    with pytest.raises(ValueError, match="duplicate schema key"):
        SchemaRegistry((registration, registration))

    duplicate_model = SchemaRegistration(
        key="second",
        model=First,
        orm_model=Namespace,
        hydrator=First.model_validate,
        loader_capabilities=(),
    )
    with pytest.raises(ValueError, match="duplicate schema model"):
        SchemaRegistry((registration, duplicate_model))

    assert Second is not First


def test_registry_rejects_unknown_keys_and_models():
    with pytest.raises(KeyError, match="unknown query schema key"):
        SCHEMA_REGISTRY.by_key("missing")
    with pytest.raises(KeyError, match="unknown query schema model"):
        SCHEMA_REGISTRY.by_model(BaseModel)
