"""Flat transport records and explicit entity/relationship policies."""

from dataclasses import dataclass
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate, AttributeValue
from recap.db.namespace import Namespace
from recap.db.process import (
    ProcessRun,
    ProcessTemplate,
    ResourceAssignment,
    ResourceSlot,
)
from recap.db.resource import Property, Resource, ResourceTemplate, ResourceType
from recap.db.step import Parameter, Step, StepTemplate
from recap.schemas.attribute import (
    AttributeGroupTemplateSchema,
    AttributeTemplateSchema,
)
from recap.schemas.namespace import NamespaceSchema
from recap.schemas.process import ProcessRunSchema, ProcessTemplateSchema
from recap.schemas.resource import (
    PropertySchema,
    ResourceSchema,
    ResourceSlotSchema,
    ResourceTemplateSchema,
    ResourceTypeSchema,
)
from recap.schemas.step import ParameterSchema, StepSchema, StepTemplateSchema

EntityKind = Literal[
    "namespace",
    "resource",
    "resource_template",
    "process_run",
    "process_template",
    "step",
    "step_template",
    "attribute_group_template",
    "attribute_template",
    "property",
    "parameter",
    "resource_slot",
    "resource_type",
    "attribute_value",
    "resource_assignment",
]


class EntityRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: EntityKind
    id: UUID


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    # Keys themselves are supplied-field coverage; absent keys are not defaults.
    fields: dict[str, Any]


class RevisionedRecord(Record):
    revision: int


class NamespaceRecord(RevisionedRecord):
    kind: Literal["namespace"] = "namespace"


class ResourceRecord(RevisionedRecord):
    kind: Literal["resource"] = "resource"


class ResourceTemplateRecord(RevisionedRecord):
    kind: Literal["resource_template"] = "resource_template"


class ProcessRunRecord(RevisionedRecord):
    kind: Literal["process_run"] = "process_run"


class ProcessTemplateRecord(RevisionedRecord):
    kind: Literal["process_template"] = "process_template"


class StepRecord(Record):
    kind: Literal["step"] = "step"


class StepTemplateRecord(Record):
    kind: Literal["step_template"] = "step_template"


class AttributeGroupRecord(Record):
    kind: Literal["attribute_group_template"] = "attribute_group_template"


class AttributeTemplateRecord(Record):
    kind: Literal["attribute_template"] = "attribute_template"


class PropertyRecord(Record):
    kind: Literal["property"] = "property"


class ParameterRecord(Record):
    kind: Literal["parameter"] = "parameter"


class SlotRecord(Record):
    kind: Literal["resource_slot"] = "resource_slot"


class TypeRecord(Record):
    kind: Literal["resource_type"] = "resource_type"


class ValueRecord(Record):
    kind: Literal["attribute_value"] = "attribute_value"


class AssignmentRecord(Record):
    """Persisted assignment row; its role-keyed public views live in adjacency."""

    kind: Literal["resource_assignment"] = "resource_assignment"


EntityRecord = Annotated[
    NamespaceRecord
    | ResourceRecord
    | ResourceTemplateRecord
    | ProcessRunRecord
    | ProcessTemplateRecord
    | StepRecord
    | StepTemplateRecord
    | AttributeGroupRecord
    | AttributeTemplateRecord
    | PropertyRecord
    | ParameterRecord
    | SlotRecord
    | TypeRecord
    | ValueRecord
    | AssignmentRecord,
    Field(discriminator="kind"),
]


class Adjacency(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: EntityRef
    relation: str
    loaded: bool = True
    complete: bool = True


class SingleAdjacency(Adjacency):
    shape: Literal["single"] = "single"
    target: EntityRef | None

    def references(self) -> list[EntityRef]:
        return [] if self.target is None else [self.target]


class NamedAdjacency(Adjacency):
    shape: Literal["named"] = "named"
    targets: dict[str, EntityRef]

    def references(self) -> list[EntityRef]:
        return list(self.targets.values())


class SequenceAdjacency(Adjacency):
    shape: Literal["sequence"] = "sequence"
    targets: list[EntityRef]

    def references(self) -> list[EntityRef]:
        return self.targets


AdjacencyRecord = Annotated[
    SingleAdjacency | NamedAdjacency | SequenceAdjacency, Field(discriminator="shape")
]


class EntityBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roots: list[EntityRef]
    entities: list[EntityRecord]
    adjacency: list[AdjacencyRecord]
    on_unloaded: Literal["silent", "warn", "raise"] = "warn"


@dataclass(frozen=True)
class RecordPolicy:
    kind: EntityKind
    orm: type
    schema: type[BaseModel] | None
    record: type[Record]
    relations: frozenset[str]
    ownership: Literal["canonical", "owned", "supporting"]


# Addressability in a batch does not extend the public client's identity families.
RECORD_POLICIES = (
    RecordPolicy(
        "namespace",
        Namespace,
        NamespaceSchema,
        NamespaceRecord,
        frozenset(),
        "canonical",
    ),
    RecordPolicy(
        "resource",
        Resource,
        ResourceSchema,
        ResourceRecord,
        frozenset({"template", "parent", "children", "properties"}),
        "canonical",
    ),
    RecordPolicy(
        "resource_template",
        ResourceTemplate,
        ResourceTemplateSchema,
        ResourceTemplateRecord,
        frozenset({"types", "parent", "children", "attribute_group_templates"}),
        "canonical",
    ),
    RecordPolicy(
        "process_run",
        ProcessRun,
        ProcessRunSchema,
        ProcessRunRecord,
        frozenset(
            {"template", "steps", "assigned_resources", "steps.parameters", "resources"}
        ),
        "canonical",
    ),
    RecordPolicy(
        "process_template",
        ProcessTemplate,
        ProcessTemplateSchema,
        ProcessTemplateRecord,
        frozenset({"step_templates", "resource_slots"}),
        "canonical",
    ),
    RecordPolicy(
        "step",
        Step,
        StepSchema,
        StepRecord,
        frozenset(
            {"template", "parameters", "children", "resources", "resource_overrides"}
        ),
        "canonical",
    ),
    RecordPolicy(
        "step_template",
        StepTemplate,
        StepTemplateSchema,
        StepTemplateRecord,
        frozenset({"attribute_group_templates", "resource_slots"}),
        "canonical",
    ),
    RecordPolicy(
        "attribute_group_template",
        AttributeGroupTemplate,
        AttributeGroupTemplateSchema,
        AttributeGroupRecord,
        frozenset({"attribute_templates"}),
        "canonical",
    ),
    RecordPolicy(
        "attribute_template",
        AttributeTemplate,
        AttributeTemplateSchema,
        AttributeTemplateRecord,
        frozenset(),
        "supporting",
    ),
    RecordPolicy(
        "property",
        Property,
        PropertySchema,
        PropertyRecord,
        frozenset({"template", "values"}),
        "owned",
    ),
    RecordPolicy(
        "parameter",
        Parameter,
        ParameterSchema,
        ParameterRecord,
        frozenset({"template", "values"}),
        "owned",
    ),
    RecordPolicy(
        "resource_slot",
        ResourceSlot,
        ResourceSlotSchema,
        SlotRecord,
        frozenset({"resource_type"}),
        "supporting",
    ),
    RecordPolicy(
        "resource_type",
        ResourceType,
        ResourceTypeSchema,
        TypeRecord,
        frozenset(),
        "supporting",
    ),
    RecordPolicy(
        "attribute_value",
        AttributeValue,
        None,
        ValueRecord,
        frozenset({"template"}),
        "owned",
    ),
    RecordPolicy(
        "resource_assignment",
        ResourceAssignment,
        None,
        AssignmentRecord,
        frozenset({"slot", "resource"}),
        "owned",
    ),
)
POLICIES_BY_ORM = {policy.orm: policy for policy in RECORD_POLICIES}
POLICIES_BY_KIND = {policy.kind: policy for policy in RECORD_POLICIES}
