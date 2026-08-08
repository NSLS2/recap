# ruff: noqa: E402
# Direction must be defined before imports that use it to avoid initialization cycles.
import enum

from ._version import __version__


class Direction(str, enum.Enum):
    """Direction of resource flow through a process step."""

    input = "input"
    output = "output"


from .client import RecapClient
from .client.permissions import (
    ActorPermissions,
    DenialCode,
    EffectivePermissions,
    PermissionDecision,
)
from .dsl.attribute_builder import AttributeGroupBuilder
from .dsl.process_builder import (
    ProcessRunBuilder,
    ProcessTemplateBuilder,
    StepTemplateBuilder,
)
from .dsl.query import (
    BaseQuery,
    Field,
    FieldOrdering,
    FieldPredicate,
    NamespaceQuery,
    ProcessRunQuery,
    ProcessTemplateQuery,
    QueryDSL,
    ResourceQuery,
    ResourceTemplateQuery,
)
from .dsl.resource_builder import ResourceBuilder, ResourceTemplateBuilder
from .lifecycle import LifecycleStatus, validate_transition
from .schemas.attribute import (
    AttributeGroupRef,
    AttributeGroupTemplateSchema,
    AttributeTemplateSchema,
    AttributeTemplateValidator,
    AttributeValueSchema,
)
from .schemas.namespace import NamespaceContext, NamespaceRef, NamespaceSchema
from .schemas.process import (
    ProcessRunRef,
    ProcessRunSchema,
    ProcessTemplateRef,
    ProcessTemplateSchema,
)
from .schemas.resource import (
    ResourceAssignmentSchema,
    ResourceCopyChanges,
    ResourceCopyOptions,
    ResourceRef,
    ResourceSchema,
    ResourceTemplateRef,
    ResourceTemplateSchema,
    ResourceTypeSchema,
)
from .schemas.step import (
    ParameterSchema,
    StepSchema,
    StepTemplateRef,
    StepTemplateSchema,
)

__all__ = [
    "__version__",
    "Direction",
    "Field",
    "LifecycleStatus",
    "QueryDSL",
    "RecapClient",
    "validate_transition",
    "ActorPermissions",
    "BaseQuery",
    "DenialCode",
    "EffectivePermissions",
    "FieldOrdering",
    "FieldPredicate",
    "NamespaceQuery",
    "PermissionDecision",
    "ProcessRunBuilder",
    "ProcessRunQuery",
    "ProcessTemplateBuilder",
    "ProcessTemplateQuery",
    "ResourceBuilder",
    "ResourceQuery",
    "ResourceTemplateBuilder",
    "ResourceTemplateQuery",
    "StepTemplateBuilder",
    "AttributeGroupBuilder",
    "AttributeGroupRef",
    "AttributeGroupTemplateSchema",
    "AttributeTemplateSchema",
    "AttributeTemplateValidator",
    "AttributeValueSchema",
    "NamespaceContext",
    "NamespaceRef",
    "NamespaceSchema",
    "ParameterSchema",
    "ProcessRunRef",
    "ProcessRunSchema",
    "ProcessTemplateRef",
    "ProcessTemplateSchema",
    "ResourceAssignmentSchema",
    "ResourceCopyChanges",
    "ResourceCopyOptions",
    "ResourceRef",
    "ResourceSchema",
    "ResourceTemplateRef",
    "ResourceTemplateSchema",
    "ResourceTypeSchema",
    "StepSchema",
    "StepTemplateRef",
    "StepTemplateSchema",
]

_LAZY_EXPORTS = {
    "ActorPermissions": (".client.permissions", "ActorPermissions"),
    "DenialCode": (".client.permissions", "DenialCode"),
    "EffectivePermissions": (".client.permissions", "EffectivePermissions"),
    "PermissionDecision": (".client.permissions", "PermissionDecision"),
    "BaseQuery": (".dsl.query", "BaseQuery"),
    "FieldOrdering": (".dsl.query", "FieldOrdering"),
    "FieldPredicate": (".dsl.query", "FieldPredicate"),
    "NamespaceQuery": (".dsl.query", "NamespaceQuery"),
    "ProcessRunQuery": (".dsl.query", "ProcessRunQuery"),
    "ProcessTemplateQuery": (".dsl.query", "ProcessTemplateQuery"),
    "ResourceQuery": (".dsl.query", "ResourceQuery"),
    "ResourceTemplateQuery": (".dsl.query", "ResourceTemplateQuery"),
    "ProcessRunBuilder": (".dsl.process_builder", "ProcessRunBuilder"),
    "ProcessTemplateBuilder": (".dsl.process_builder", "ProcessTemplateBuilder"),
    "StepTemplateBuilder": (".dsl.process_builder", "StepTemplateBuilder"),
    "ResourceBuilder": (".dsl.resource_builder", "ResourceBuilder"),
    "ResourceTemplateBuilder": (".dsl.resource_builder", "ResourceTemplateBuilder"),
    "AttributeGroupBuilder": (".dsl.attribute_builder", "AttributeGroupBuilder"),
}


def __getattr__(name):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    from importlib import import_module

    value = getattr(import_module(target[0], __name__), target[1])
    globals()[name] = value
    return value
