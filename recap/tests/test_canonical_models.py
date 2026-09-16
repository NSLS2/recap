import warnings
from datetime import datetime
from uuid import uuid4

import pytest

from recap.dsl.query import QueryDSL
from recap.exceptions import UnloadedFieldError
from recap.lifecycle import LifecycleStatus
from recap.schemas.attribute import (
    AttributeGroupRef,
    AttributeGroupTemplate,
    AttributeGroupTemplateRef,
    AttributeGroupTemplateSchema,
)
from recap.schemas.namespace import NamespaceContext
from recap.schemas.process import (
    ProcessRunSchema,
    ProcessTemplateSchema,
)
from recap.schemas.resource import ResourceRef, ResourceSchema, ResourceTemplateRef
from recap.schemas.step import (
    StepSchema,
    StepTemplate,
    StepTemplateRef,
    StepTemplateSchema,
)


def _fields(**extra):
    return {
        "id": uuid4(),
        "create_date": datetime(2026, 1, 1),
        "modified_date": datetime(2026, 1, 1),
        "namespace_id": uuid4(),
        "status": LifecycleStatus.ACTIVE,
        "revision": 1,
        **extra,
    }


def test_ref_names_are_canonical_models_with_unloaded_relation_defaults():
    assert ResourceRef is ResourceSchema
    assert ResourceTemplateRef.__name__ == "ResourceTemplateSchema"
    resource = ResourceRef.model_validate(_fields(name="sample"))
    assert isinstance(resource, ResourceSchema)
    assert resource.children == {}
    assert resource.properties == {}
    assert resource.is_loaded("children") is False


def test_public_template_ref_aliases_share_canonical_identity_families():
    assert AttributeGroupRef is AttributeGroupTemplateSchema
    assert AttributeGroupTemplate is AttributeGroupTemplateSchema
    assert AttributeGroupTemplateRef is AttributeGroupTemplateSchema
    assert StepTemplate is StepTemplateSchema
    assert StepTemplateRef is StepTemplateSchema


@pytest.mark.parametrize(
    ("model", "relation"),
    [
        (ResourceSchema, "template"),
        (ResourceSchema, "parent"),
        (ProcessRunSchema, "template"),
    ],
)
def test_canonical_models_guard_unloaded_template_relations(model, relation):
    value = model.model_construct()
    value.set_loaded_relations({relation: False}, on_unloaded="raise")

    with pytest.raises(UnloadedFieldError):
        getattr(value, relation)


def test_recursive_relations_use_canonical_classes():
    template = ResourceTemplateRef.model_validate(
        _fields(name="sample", version="1.0", labels=[])
    )
    resource = ResourceSchema.model_validate(_fields(name="sample", template=template))
    assert isinstance(resource.template, ResourceTemplateRef)
    assert ResourceTemplateRef is type(resource.template)


def _query():
    return QueryDSL(object(), context=NamespaceContext(id=uuid4(), path="beamline"))


def test_deprecated_ref_shape_and_expand_return_canonical_models():
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        query = _query().resources(shape="ref")
    assert any(item.category is DeprecationWarning for item in seen)
    assert query.model is ResourceSchema
    assert query._spec.load_mode == "none"


def test_deprecated_expand_warns_and_maps_to_eager():
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        query = _query().resources(expand=True)
    assert any(item.category is DeprecationWarning for item in seen)
    assert query._spec.load_mode == "eager"


@pytest.mark.parametrize(
    ("model", "relation"),
    [
        (ResourceTemplateRef, "children"),
        (ProcessTemplateSchema, "step_templates"),
        (StepTemplateSchema, "attribute_group_templates"),
        (StepSchema, "parameters"),
    ],
)
def test_canonical_relation_models_guard_unloaded_relations(model, relation):
    value = model.model_construct()
    value.set_loaded_relations({relation: False}, on_unloaded="raise")

    with pytest.raises(UnloadedFieldError):
        getattr(value, relation)
