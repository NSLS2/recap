__all__ = [
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

_MODULES = {
    "AttributeGroupRef": ".attribute",
    "AttributeGroupTemplateSchema": ".attribute",
    "AttributeTemplateSchema": ".attribute",
    "AttributeTemplateValidator": ".attribute",
    "AttributeValueSchema": ".attribute",
    "NamespaceContext": ".namespace",
    "NamespaceRef": ".namespace",
    "NamespaceSchema": ".namespace",
    "ParameterSchema": ".step",
    "ProcessRunRef": ".process",
    "ProcessRunSchema": ".process",
    "ProcessTemplateRef": ".process",
    "ProcessTemplateSchema": ".process",
    "ResourceAssignmentSchema": ".resource",
    "ResourceCopyChanges": ".resource",
    "ResourceCopyOptions": ".resource",
    "ResourceRef": ".resource",
    "ResourceSchema": ".resource",
    "ResourceTemplateRef": ".resource",
    "ResourceTemplateSchema": ".resource",
    "ResourceTypeSchema": ".resource",
    "StepSchema": ".step",
    "StepTemplateRef": ".step",
    "StepTemplateSchema": ".step",
}


def __getattr__(name):
    module_name = _MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    from importlib import import_module

    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value
