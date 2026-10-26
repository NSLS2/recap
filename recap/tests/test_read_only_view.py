import copy

import pytest

from recap.schemas.attribute import AttributeValueSchema
from recap.schemas.resource import ResourceSchema
from recap.utils import dsl

from .transport_factories import full_resource


def test_view_reads_canonical_refresh_and_rejects_writes():
    model = full_resource()
    view = dsl.read_only_view(model)
    assert isinstance(view, ResourceSchema)
    assert view == model
    assert model == view
    model.name = "updated"
    assert view.name == "updated"
    with pytest.raises(TypeError, match="read-only"):
        view.name = "unsaved"
    with pytest.raises(TypeError, match="read-only"):
        del view.name


def test_view_protects_nested_models_and_mutable_values():
    model = full_resource()
    view = dsl.read_only_view(model)
    with pytest.raises(TypeError, match="read-only"):
        view.template.name = "unsaved"
    with pytest.raises(TypeError):
        view.children["new"] = model
    with pytest.raises(TypeError, match="read-only"):
        view.children["child"].name = "unsaved"
    value = AttributeValueSchema(value=[{"x": [1]}], metadata_json={"source": ["old"]})
    frozen = dsl.read_only_view(value)
    with pytest.raises(TypeError):
        frozen.value[0]["x"][0] = 2
    with pytest.raises(TypeError):
        frozen.metadata_json["source"][0] = "new"
    assert value.value == [{"x": [1]}]


def test_view_copy_serialization_and_loaded_flags():
    model = full_resource()
    view = dsl.read_only_view(model)
    assert dsl.read_only_view(view) is view
    assert copy.copy(view) is view
    assert copy.deepcopy(view) is view
    assert view.model_dump() == model.model_dump()
    assert view.model_dump_json() == model.model_dump_json()
    assert (
        view.template.attribute_group_templates
        == model.template.attribute_group_templates
    )
    assert (
        view.template.attribute_group_templates[:]
        == model.template.attribute_group_templates
    )
    assert view.is_loaded("children")
    copied = view.model_copy(deep=True, update={"name": "copy"})
    assert type(copied) is ResourceSchema
    copied.template.name = "independent"
    assert model.template.name == "Sample"


def test_view_alias_access_cannot_mutate_canonical():
    model = full_resource()
    view = dsl.read_only_view(model)
    with pytest.raises(TypeError, match="read-only"):
        view.properties.measurements["Captured At"] = None
    with pytest.raises(TypeError, match="read-only"):
        view.properties.measurements.captured_at.unit = "s"
    with pytest.raises(TypeError, match="read-only"):
        view.properties.measurements.get("Captured At").unit = "s"
    with pytest.raises(TypeError, match="read-only"):
        next(view.properties.measurements.items())[1].unit = "s"
    assert model.properties.measurements.captured_at.value is not None
