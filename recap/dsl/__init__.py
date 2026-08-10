"""Public query and builder APIs.

Imports are resolved lazily to keep adapter and schema initialization acyclic.
"""

__all__ = [
    "AttributeGroupBuilder",
    "BaseQuery",
    "DraftAttributeGroupBuilder",
    "Field",
    "FieldOrdering",
    "FieldPredicate",
    "NamespaceQuery",
    "ProcessRunBuilder",
    "ProcessRunQuery",
    "ProcessTemplateBuilder",
    "ProcessTemplateQuery",
    "QueryDSL",
    "ResourceBuilder",
    "ResourceQuery",
    "ResourceTemplateBuilder",
    "ResourceTemplateQuery",
    "StepTemplateBuilder",
]

_MODULES = {
    "AttributeGroupBuilder": ".attribute_builder",
    "BaseQuery": ".query",
    "Field": ".query",
    "FieldOrdering": ".query",
    "FieldPredicate": ".query",
    "NamespaceQuery": ".query",
    "ProcessRunQuery": ".query",
    "ProcessTemplateQuery": ".query",
    "QueryDSL": ".query",
    "ResourceQuery": ".query",
    "ResourceTemplateQuery": ".query",
    "DraftAttributeGroupBuilder": ".process_builder",
    "ProcessRunBuilder": ".process_builder",
    "ProcessTemplateBuilder": ".process_builder",
    "StepTemplateBuilder": ".process_builder",
    "ResourceBuilder": ".resource_builder",
    "ResourceTemplateBuilder": ".resource_builder",
}


def __getattr__(name):
    module_name = _MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    from importlib import import_module

    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value
