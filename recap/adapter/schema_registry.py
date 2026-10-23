from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import BaseModel

from recap.adapter.entity_records import POLICIES_BY_KIND, RecordPolicy
from recap.db.base import Base
from recap.db.namespace import Namespace
from recap.db.process import ProcessRun, ProcessTemplate
from recap.db.resource import Resource, ResourceTemplate
from recap.schemas.namespace import NamespaceSchema
from recap.schemas.process import (
    ProcessRunSchema,
    ProcessTemplateSchema,
)
from recap.schemas.resource import (
    ResourceSchema,
    ResourceTemplateSchema,
)

SCHEMA_QUERY_KEYS: dict[type[BaseModel], tuple[str, str]] = {
    NamespaceSchema: ("namespace", "full"),
    ResourceTemplateSchema: ("resource_template", "full"),
    ResourceSchema: ("resource", "full"),
    ProcessTemplateSchema: ("process_template", "full"),
    ProcessRunSchema: ("process_run", "full"),
}

QUERY_SCHEMA_KEYS: dict[tuple[str, str], type[BaseModel]] = {
    (entity, projection): schema
    for schema, (entity, projection) in SCHEMA_QUERY_KEYS.items()
}
for entity, schema in (
    ("namespace", NamespaceSchema),
    ("resource_template", ResourceTemplateSchema),
    ("resource", ResourceSchema),
    ("process_template", ProcessTemplateSchema),
    ("process_run", ProcessRunSchema),
):
    QUERY_SCHEMA_KEYS[(entity, "ref")] = schema

QUERY_ENTITY_KEYS = {
    schema: entity for schema, (entity, _) in SCHEMA_QUERY_KEYS.items()
}

QUERY_PROJECTION_SCHEMAS: dict[str, dict[str, type[BaseModel]]] = {
    "namespace": {"full": NamespaceSchema, "ref": NamespaceSchema},
    "resource_template": {
        "full": ResourceTemplateSchema,
        "ref": ResourceTemplateSchema,
    },
    "resource": {"full": ResourceSchema, "ref": ResourceSchema},
    "process_template": {"full": ProcessTemplateSchema, "ref": ProcessTemplateSchema},
    "process_run": {"full": ProcessRunSchema, "ref": ProcessRunSchema},
}

SCHEMA_ENTITY_KEYS = QUERY_ENTITY_KEYS
SCHEMA_PROJECTIONS = {
    schema: projection for schema, (_, projection) in SCHEMA_QUERY_KEYS.items()
}


@dataclass(frozen=True)
class SchemaRegistration:
    key: str
    model: type[BaseModel]
    orm_model: type[Base]
    loader_capabilities: tuple[str, ...]
    projections: tuple[str, ...] = ("full", "ref")

    @property
    def record_policy(self) -> RecordPolicy:
        """Normalized row policy used by query collection and materialization."""
        return POLICIES_BY_KIND[self.key]


class SchemaRegistry:
    def __init__(self, registrations: Iterable[SchemaRegistration]):
        self._by_key: dict[str, SchemaRegistration] = {}
        self._by_model: dict[type[BaseModel], SchemaRegistration] = {}
        for registration in registrations:
            if registration.key in self._by_key:
                raise ValueError(f"duplicate schema key: {registration.key}")
            if registration.model in self._by_model:
                raise ValueError(
                    f"duplicate schema model: {registration.model.__name__}"
                )
            self._by_key[registration.key] = registration
            self._by_model[registration.model] = registration

    def keys(self):
        return self._by_key.keys()

    def by_key(self, key: str) -> SchemaRegistration:
        try:
            return self._by_key[key]
        except KeyError as exc:
            raise KeyError(f"unknown query schema key: {key}") from exc

    def by_model(self, model: type[BaseModel]) -> SchemaRegistration:
        try:
            return self._by_model[model]
        except KeyError as exc:
            raise KeyError(f"unknown query schema model: {model.__name__}") from exc

    def validate_complete(self) -> None:
        from recap.adapter.query_loaders import PRELOAD_STATEMENTS

        for registration in self._by_key.values():
            declared = set(registration.loader_capabilities)
            actual = {
                relation
                for (schema, relation), statements in PRELOAD_STATEMENTS.items()
                if schema is registration.model and statements
            }
            if actual != declared:
                raise ValueError(
                    f"incomplete loader capabilities for {registration.key}: "
                    f"declared {sorted(declared)}, actual {sorted(actual)}"
                )


SCHEMA_REGISTRY = SchemaRegistry(
    (
        SchemaRegistration("namespace", NamespaceSchema, Namespace, ()),
        SchemaRegistration(
            "resource_template",
            ResourceTemplateSchema,
            ResourceTemplate,
            ("types", "children", "attribute_group_templates"),
        ),
        SchemaRegistration(
            "resource",
            ResourceSchema,
            Resource,
            ("template", "properties", "children"),
        ),
        SchemaRegistration(
            "process_template",
            ProcessTemplateSchema,
            ProcessTemplate,
            ("step_templates", "resource_slots"),
        ),
        SchemaRegistration(
            "process_run",
            ProcessRunSchema,
            ProcessRun,
            ("template", "steps", "steps.parameters", "resources"),
        ),
    )
)


def schema_for(entity: str, projection: str) -> type[BaseModel]:
    try:
        registration = SCHEMA_REGISTRY.by_key(entity)
        if projection not in registration.projections:
            raise KeyError(projection)
        return registration.model
    except KeyError as exc:
        raise ValueError(
            f"Unknown query entity/projection: {entity}/{projection}"
        ) from exc
