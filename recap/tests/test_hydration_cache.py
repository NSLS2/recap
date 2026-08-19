from unittest.mock import patch
from uuid import uuid4

import pytest

from recap.adapter.entity_hydration import EntityHydrationContext
from recap.adapter.process_run_construct import ProcessRunSchemaHydrator
from recap.adapter.resource_construct import ResourceSchemaHydrator
from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.db.namespace import Namespace
from recap.db.process import ProcessRun, ProcessTemplate
from recap.db.resource import (
    ROOT_RESOURCE_TEMPLATE_ID,
    Resource,
    ResourceTemplate,
    ResourceType,
)
from recap.db.step import StepTemplate


def _seed_resource_graph(db_session):
    namespace = Namespace(path=f"cache/{uuid4().hex}", metadata_json={})
    resource_type = ResourceType(name=f"cache-type-{uuid4().hex}")
    parent_template = ResourceTemplate(
        name=f"ParentTemplate-{uuid4().hex}",
        namespace=namespace,
    )
    parent_template.types.append(resource_type)
    property_group = AttributeGroupTemplate(name="Measurements")
    property_group.attribute_templates.append(
        AttributeTemplate(name="dose", value_type="int", default_value="1")
    )
    parent_template.attribute_group_templates.append(property_group)
    child_template = ResourceTemplate(
        name="child",
        namespace=namespace,
        parent=parent_template,
    )
    parent = Resource(
        name="parent",
        template=parent_template,
        namespace=namespace,
    )
    child = parent.children["child"]
    db_session.add_all(
        [
            namespace,
            resource_type,
            parent_template,
            child_template,
            parent,
        ]
    )
    db_session.flush()
    return parent, child, parent_template, child_template


def _seed_process_template(db_session):
    namespace = Namespace(path=f"cache/process-{uuid4().hex}", metadata_json={})
    process_template = ProcessTemplate(
        name=f"Process-{uuid4().hex}",
        version="1.0",
        namespace=namespace,
    )
    step_template = StepTemplate(name="Collect", process_template=process_template)
    parameter_group = AttributeGroupTemplate(name="Exposure")
    parameter_group.attribute_templates.append(
        AttributeTemplate(name="dwell", value_type="int", default_value="1")
    )
    step_template.attribute_group_templates.append(parameter_group)
    db_session.add_all([namespace, process_template, step_template])
    db_session.flush()
    return process_template, step_template


def test_hydration_context_tracks_one_cache_and_recursion_guard_per_family():
    context = EntityHydrationContext()
    entity_id = uuid4()

    assert context.resource_cache == {}
    assert context.resource_template_cache == {}
    assert not context.in_progress("resource", entity_id)

    context.begin("resource", entity_id)
    assert context.in_progress("resource", entity_id)

    context.finish("resource", entity_id)
    assert not context.in_progress("resource", entity_id)


def test_merge_load_state_unions_flags_without_downgrade(db_session):
    parent, _, _, _ = _seed_resource_graph(db_session)
    context = EntityHydrationContext()
    model = ResourceSchemaHydrator(context).construct_many(
        [parent],
        include_template=True,
        include_properties=False,
        include_children=False,
        full=False,
        on_unloaded="raise",
    )[0]

    context.merge_load_state(
        model,
        {"template": False, "properties": True},
        on_unloaded="raise",
    )

    assert model.is_loaded("template")
    assert model.is_loaded("properties")
    assert not model.is_loaded("children")


def test_resource_cache_upgrades_partial_to_full_in_place(db_session):
    parent, _, _, _ = _seed_resource_graph(db_session)
    context = EntityHydrationContext()
    hydrator = ResourceSchemaHydrator(context)

    partial = hydrator.construct_many(
        [parent],
        include_template=False,
        include_properties=False,
        include_children=False,
        full=False,
        on_unloaded="raise",
    )[0]
    full = hydrator.construct_many(
        [parent],
        include_template=True,
        include_properties=True,
        include_children=True,
        full=True,
        on_unloaded="raise",
    )[0]

    assert full is partial
    assert full.is_loaded("template")
    assert full.is_loaded("parent")
    assert full.is_loaded("children")
    assert full.is_loaded("properties")
    assert set(full.children) == {"child"}
    assert full.properties.measurements.values.dose.value == 1


def test_resource_cache_does_not_downgrade_full_model(db_session):
    parent, _, _, _ = _seed_resource_graph(db_session)
    context = EntityHydrationContext()
    hydrator = ResourceSchemaHydrator(context)

    full = hydrator.construct_many(
        [parent],
        include_template=True,
        include_properties=True,
        include_children=True,
        full=True,
        on_unloaded="raise",
    )[0]
    original_children = full.children
    partial = hydrator.construct_many(
        [parent],
        include_template=False,
        include_properties=False,
        include_children=False,
        full=False,
        on_unloaded="raise",
    )[0]

    assert partial is full
    assert partial.children is original_children
    assert partial.is_loaded("template")
    assert partial.is_loaded("parent")
    assert partial.is_loaded("children")
    assert partial.is_loaded("properties")


def test_repeated_resource_occurrences_create_one_cached_object(db_session):
    parent, child, _, _ = _seed_resource_graph(db_session)
    context = EntityHydrationContext()
    hydrator = ResourceSchemaHydrator(context)

    first_parent, second_parent, direct_child = hydrator.construct_many(
        [parent, parent, child],
        include_template=True,
        include_properties=True,
        include_children=True,
        full=True,
        on_unloaded="raise",
    )

    assert first_parent is second_parent
    assert first_parent.children["child"] is direct_child
    assert set(context.resource_cache) == {parent.id, child.id}
    assert set(context.resource_template_cache) == {
        ROOT_RESOURCE_TEMPLATE_ID,
        parent.template.id,
        child.template.id,
    }


def test_parent_seen_as_ref_then_root_upgrades_same_resource(db_session):
    parent, child, _, _ = _seed_resource_graph(db_session)
    context = EntityHydrationContext()
    hydrator = ResourceSchemaHydrator(context)

    child_model = hydrator.construct_many(
        [child],
        include_template=True,
        include_properties=True,
        include_children=True,
        full=True,
        on_unloaded="raise",
    )[0]
    parent_ref = child_model.parent
    parent_model = hydrator.construct_many(
        [parent],
        include_template=True,
        include_properties=True,
        include_children=True,
        full=True,
        on_unloaded="raise",
    )[0]

    assert parent_model is parent_ref
    assert parent_model.is_loaded("children")
    assert parent_model.children["child"] is child_model
    assert parent_model.is_loaded("properties")


def test_resource_template_ref_then_full_upgrades_same_object(db_session):
    _, _, parent_template, _ = _seed_resource_graph(db_session)
    context = EntityHydrationContext()
    hydrator = ResourceSchemaHydrator(context)

    ref = hydrator._construct_resource_template_ref(
        parent_template,
        on_unloaded="raise",
    )
    full = hydrator._construct_resource_template(
        parent_template,
        on_unloaded="raise",
    )

    assert full is ref
    assert full.is_loaded("parent")
    assert full.is_loaded("types")
    assert full.is_loaded("children")
    assert full.is_loaded("attribute_group_templates")
    assert set(full.children) == set(parent_template.children)
    assert [group.name for group in full.attribute_group_templates] == ["Measurements"]


def test_resource_template_full_then_minimal_preserves_loaded_relations(db_session):
    _, _, parent_template, _ = _seed_resource_graph(db_session)
    context = EntityHydrationContext()
    hydrator = ResourceSchemaHydrator(context)

    full = hydrator._construct_resource_template(
        parent_template,
        on_unloaded="raise",
    )
    original_children = full.children
    minimal = hydrator._construct_resource_template_minimal(
        parent_template,
        on_unloaded="raise",
    )

    assert minimal is full
    assert minimal.children is original_children
    assert minimal.is_loaded("types")
    assert minimal.is_loaded("parent")
    assert minimal.is_loaded("children")
    assert minimal.is_loaded("attribute_group_templates")


def test_resource_template_parent_reuses_canonical_template(db_session):
    _, _, parent_template, child_template = _seed_resource_graph(db_session)
    context = EntityHydrationContext()
    hydrator = ResourceSchemaHydrator(context)

    child = hydrator._construct_resource_template(
        child_template,
        on_unloaded="raise",
    )
    parent = hydrator._construct_resource_template(
        parent_template,
        on_unloaded="raise",
    )

    assert child.parent is parent


def test_separate_hydration_contexts_do_not_share_identity(db_session):
    parent, _, _, _ = _seed_resource_graph(db_session)
    first = ResourceSchemaHydrator(EntityHydrationContext()).construct_many(
        [parent],
        include_template=True,
        include_properties=False,
        include_children=False,
        full=False,
        on_unloaded="raise",
    )[0]
    second = ResourceSchemaHydrator(EntityHydrationContext()).construct_many(
        [parent],
        include_template=True,
        include_properties=False,
        include_children=False,
        full=False,
        on_unloaded="raise",
    )[0]

    assert first is not second
    assert first.template is not second.template


def test_process_hydrator_reuses_one_resource_hydrator_and_cache(db_session):
    parent, _, _, _ = _seed_resource_graph(db_session)
    context = EntityHydrationContext()
    process_hydrator = ProcessRunSchemaHydrator(context)

    first = process_hydrator._construct_resource_schema(parent)
    second = process_hydrator._construct_resource_schema(parent)

    assert first is second
    assert set(context.resource_cache) == {
        parent.id,
        *[child.id for child in parent.children.values()],
    }


def test_process_template_minimal_then_full_upgrades_in_place(db_session):
    process_template, _ = _seed_process_template(db_session)
    context = EntityHydrationContext()

    minimal = context.construct_process_template(
        process_template,
        include_relations=False,
        on_unloaded="raise",
    )
    full = context.construct_process_template(
        process_template,
        include_relations=True,
        on_unloaded="raise",
    )

    assert full is minimal
    assert full.is_loaded("step_templates")
    assert full.is_loaded("resource_slots")
    assert set(full.step_templates) == {"Collect"}


def test_step_template_minimal_then_full_upgrades_in_place(db_session):
    _, step_template = _seed_process_template(db_session)
    context = EntityHydrationContext()

    minimal = context.construct_step_template(
        step_template,
        include_relations=False,
        on_unloaded="raise",
    )
    full = context.construct_step_template(
        step_template,
        include_relations=True,
        on_unloaded="raise",
    )

    assert full is minimal
    assert full.is_loaded("attribute_group_templates")
    assert full.is_loaded("resource_slots")
    assert [group.name for group in full.attribute_group_templates] == ["Exposure"]


def test_process_run_nested_templates_preserve_unloaded_policy(db_session):
    process_template, _ = _seed_process_template(db_session)
    run = ProcessRun(
        name=f"run-{uuid4().hex}",
        description="nested policy",
        template=process_template,
        namespace=process_template.namespace,
    )
    db_session.add(run)
    db_session.flush()

    [hydrated] = ProcessRunSchemaHydrator().construct_many(
        [run],
        include_steps=True,
        include_step_parameters=False,
        include_resources=False,
        include_template=True,
        full=True,
        on_unloaded="raise",
    )

    assert hydrated.template._on_unloaded == "raise"
    assert hydrated.template.step_templates["Collect"]._on_unloaded == "raise"


def test_runtime_ref_aliases_are_canonical_schema_classes():
    from recap.schemas.attribute import (
        AttributeGroupRef,
        AttributeGroupTemplateSchema,
    )
    from recap.schemas.process import (
        ProcessRunRef,
        ProcessRunSchema,
        ProcessTemplateRef,
        ProcessTemplateSchema,
    )
    from recap.schemas.resource import (
        ResourceRef,
        ResourceSchema,
        ResourceTemplateRef,
        ResourceTemplateSchema,
    )
    from recap.schemas.step import StepTemplateRef, StepTemplateSchema

    assert AttributeGroupRef is AttributeGroupTemplateSchema
    assert ProcessRunRef is ProcessRunSchema
    assert ProcessTemplateRef is ProcessTemplateSchema
    assert ResourceRef is ResourceSchema
    assert ResourceTemplateRef is ResourceTemplateSchema
    assert StepTemplateRef is StepTemplateSchema


def test_resource_recursion_guard_returns_cached_shell(db_session):
    parent, _, _, _ = _seed_resource_graph(db_session)
    context = EntityHydrationContext()
    hydrator = ResourceSchemaHydrator(context)

    shell = hydrator.construct_many(
        [parent],
        include_template=False,
        include_properties=False,
        include_children=False,
        full=False,
        on_unloaded="raise",
    )[0]
    context.begin("resource", parent.id)
    try:
        recursive = hydrator._construct_resource_schema(
            parent,
            include_template=True,
            include_properties=True,
            include_children=True,
            full=True,
            on_unloaded="raise",
        )
    finally:
        context.finish("resource", parent.id)

    assert recursive is shell
    assert not recursive.is_loaded("children")
    assert not recursive.is_loaded("properties")


def test_failed_resource_upgrade_clears_guard_and_preserves_unloaded_flag(db_session):
    parent, _, _, _ = _seed_resource_graph(db_session)
    context = EntityHydrationContext()
    hydrator = ResourceSchemaHydrator(context)
    partial = hydrator.construct_many(
        [parent],
        include_template=False,
        include_properties=False,
        include_children=False,
        full=False,
        on_unloaded="raise",
    )[0]

    with patch.object(
        hydrator,
        "_construct_property_schema",
        side_effect=RuntimeError("property hydration failed"),
    ), pytest.raises(RuntimeError, match="property hydration failed"):
        hydrator.construct_many(
            [parent],
            include_template=False,
            include_properties=True,
            include_children=False,
            full=False,
            on_unloaded="raise",
        )

    assert not context.in_progress("resource", parent.id)
    assert not partial.is_loaded("properties")
