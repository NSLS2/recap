import gc
import weakref
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import RLock
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from pydantic import Field, create_model

from recap.client.backend import ClientBackend
from recap.client.base_client import RecapClient
from recap.client.connection_state import ConnectionState
from recap.client.identity import IdentityMap, IdentityMergeConflict
from recap.schemas.attribute import (
    AttributeGroupTemplateSchema,
    AttributeTemplateSchema,
    AttributeValueSchema,
)
from recap.schemas.namespace import NamespaceContext, NamespaceRef, NamespaceSchema
from recap.schemas.resource import (
    PropertySchema,
    ResourceRef,
    ResourceSchema,
    ResourceSlotSchema,
    ResourceTemplateSchema,
    ResourceTypeSchema,
)
from recap.schemas.step import ParameterSchema
from recap.utils.dsl import build_param_values_model


def _resource(resource_id, *, name="sample", revision=1, modified_date=None):
    return ResourceSchema.model_construct(
        id=resource_id,
        create_date=datetime(2026, 1, 1),
        modified_date=modified_date or datetime(2026, 1, revision),
        namespace_id=uuid4(),
        status="ACTIVE",
        revision=revision,
        name=name,
    )


def test_same_id_ref_and_full_models_share_identity():
    identity_map = IdentityMap()
    resource_id = uuid4()
    resource = _resource(resource_id)
    reference = ResourceRef.model_construct(
        id=resource_id,
        create_date=resource.create_date,
        modified_date=resource.modified_date,
        namespace_id=resource.namespace_id,
        status=resource.status,
        revision=resource.revision,
        name=resource.name,
    )

    first = identity_map.intern(resource)
    second = identity_map.intern(reference)

    assert first is second


def test_namespace_ref_and_schema_share_identity_but_context_does_not():
    identity_map = IdentityMap()
    namespace_id = uuid4()
    namespace = NamespaceSchema.model_construct(
        id=namespace_id,
        create_date=datetime(2026, 1, 1),
        modified_date=datetime(2026, 1, 1),
        path="sample",
        parent_id=None,
        status="ACTIVE",
        revision=1,
        metadata={},
    )
    reference = NamespaceRef(id=namespace_id, path="sample")
    context = NamespaceContext(id=namespace_id, path="sample")

    canonical = identity_map.intern(namespace)

    assert identity_map.intern(reference) is canonical
    assert identity_map.intern(context) is context


def test_namespace_schema_promotes_ref_without_changing_identity():
    identity_map = IdentityMap()
    namespace_id = uuid4()
    reference = NamespaceRef(id=namespace_id, path="sample")
    schema = NamespaceSchema.model_construct(
        id=namespace_id,
        create_date=datetime(2026, 1, 1),
        modified_date=datetime(2026, 1, 1),
        path="sample",
        parent_id=None,
        status="ACTIVE",
        revision=1,
        metadata={},
    )

    canonical = identity_map.intern(reference)
    promoted = identity_map.intern(schema)

    assert promoted is canonical
    assert isinstance(canonical, NamespaceSchema)
    assert canonical.revision == 1
    assert (
        identity_map.intern(NamespaceRef(id=namespace_id, path="sample")) is canonical
    )


def test_concurrent_intern_returns_one_namespace_canonical():
    identity_map = IdentityMap()
    namespace_id = uuid4()

    def intern_reference():
        return identity_map.intern(NamespaceRef(id=namespace_id, path="sample"))

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _index: intern_reference(), range(32)))

    assert {id(result) for result in results} == {id(results[0])}


def test_identity_map_retains_model_until_clear():
    identity_map = IdentityMap()
    resource_id = uuid4()
    resource = _resource(resource_id)
    reference = weakref.ref(resource)
    identity_map.intern(resource)

    del resource
    gc.collect()
    assert reference() is not None

    identity_map.clear()
    gc.collect()
    assert reference() is None


def test_intern_handles_self_referential_persisted_graphs():
    identity_map = IdentityMap()
    resource = _resource(uuid4())
    resource.children = [resource]

    canonical = identity_map.intern(resource)

    assert canonical.children[0] is canonical


@pytest.mark.parametrize("container_type", ["model", "dict", "list"])
def test_merging_identical_container_does_not_rewalk_canonical_graph(container_type):
    identity_map = IdentityMap()
    resource = _resource(uuid4())
    if container_type == "model":
        container = resource
    elif container_type == "dict":
        container = {"sample": resource}
    else:
        container = [resource]

    with patch.object(identity_map, "_canonicalize_value") as canonicalize:
        assert identity_map._merge_container(container, container)
        canonicalize.assert_not_called()


def test_equal_revision_cyclic_template_relations_do_not_recurse():
    identity_map = IdentityMap()
    namespace_id = uuid4()
    parent_id = uuid4()
    child_id = uuid4()

    def template_graph():
        parent = ResourceTemplateSchema.model_construct(
            id=parent_id,
            namespace_id=namespace_id,
            create_date=datetime(2026, 1, 1),
            modified_date=datetime(2026, 1, 1),
            status="ACTIVE",
            revision=1,
            name="Parent",
            slug="parent",
            version="1.0",
            labels=[],
            types=[],
            children={},
            attribute_group_templates=[],
        )
        child = ResourceTemplateSchema.model_construct(
            id=child_id,
            namespace_id=namespace_id,
            create_date=datetime(2026, 1, 1),
            modified_date=datetime(2026, 1, 1),
            status="ACTIVE",
            revision=1,
            name="Child",
            slug="child",
            version="1.0",
            labels=[],
            types=[],
            children={},
            attribute_group_templates=[],
        )
        parent.children = {"child": child}
        parent.parent = None
        child.parent = parent
        parent.set_loaded_relations(
            {
                "parent": True,
                "children": True,
                "types": True,
                "attribute_group_templates": True,
            }
        )
        child.set_loaded_relations(
            {
                "parent": True,
                "children": True,
                "types": True,
                "attribute_group_templates": True,
            }
        )
        return parent

    first = identity_map.intern(template_graph())
    second = template_graph()

    assert identity_map.intern(second) is first


def test_merge_replaces_scalar_list_and_dict_fields():
    identity_map = IdentityMap()
    current = AttributeValueSchema(value=["old"], metadata_json={"old": True})
    incoming = AttributeValueSchema(value=["new"], metadata_json={"new": True})

    identity_map._merge_container(current, incoming)

    assert current.value == ["new"]
    assert current.metadata_json == {"new": True}


def test_merge_dict_view_replaces_scalar_list_and_dict_fields():
    current = AttributeValueSchema(value=["old"], metadata_json={"old": True})
    assert IdentityMap()._merge_container(
        current, {"value": ["new"], "metadata_json": {"new": True}}
    )
    assert current.value == ["new"]
    assert current.metadata_json == {"new": True}


@pytest.mark.parametrize("empty", [False, True])
def test_merge_dict_view_preserves_generated_container(empty):
    container = create_model(
        "DictViewContainer", dose=(AttributeValueSchema, Field(alias="Dose"))
    )
    dose = AttributeValueSchema(value=1)
    current = container(Dose=dose)
    incoming = {} if empty else {"Dose": AttributeValueSchema(value=None)}
    assert IdentityMap()._merge_container(current, incoming)
    assert current.dose is dose
    assert dose.value == (1 if empty else None)


@pytest.mark.parametrize("group_type", [PropertySchema, ParameterSchema])
@pytest.mark.parametrize("incoming_value", [None, 7])
@pytest.mark.parametrize("different_class", [False, True])
def test_merge_preserves_nested_generated_containers(
    group_type, incoming_value, different_class
):
    stamp = datetime(2026, 1, 1)
    template = AttributeGroupTemplateSchema(
        id=uuid4(), create_date=stamp, modified_date=stamp,
        name="Measurements", slug="measurements",
        attribute_templates=[
            AttributeTemplateSchema(
                id=uuid4(), create_date=stamp, modified_date=stamp,
                name="Dose", slug="dose", value_type="int",
                default_value=None, unit="uL", metadata={"min": 0, "max": 10},
            ),
        ],
    )
    group_id = uuid4()

    def group(value, unit, metadata, distinct=False):
        model = build_param_values_model(
            "other_measurements" if distinct else "measurements",
            (("Dose", "dose", "int", {"min": 0, "max": 10}, "uL"),),
        )
        return group_type(
            id=group_id, create_date=stamp, modified_date=stamp,
            template=template,
            values=model.model_validate({"Dose": {
                "value": value, "unit": unit, "metadata_json": metadata,
            }}),
        )

    current_group = group(1, "uL", {"old": True})
    incoming_group = group(incoming_value, "mL", {"new": True}, different_class)
    container = create_model("MeasurementsContainer", measurements=(group_type, ...))
    current = container(measurements=current_group)
    incoming_type = (
        create_model("OtherMeasurementsContainer", measurements=(group_type, ...),
                     unknown=(int, Field(default=3)))
        if different_class else container
    )
    incoming = incoming_type(measurements=incoming_group, unknown=3) if different_class else incoming_type(measurements=incoming_group)
    original_values = current_group.values
    original_value = original_values.dose
    original_type = type(original_values)

    assert IdentityMap()._merge_container(current, incoming)

    assert current.measurements is current_group
    assert current.measurements.values is original_values
    assert type(current.measurements.values) is original_type
    assert current.measurements.values.dose is original_value
    assert original_value.value == incoming_value
    assert original_value.unit == "mL"
    assert original_value.metadata_json == {"new": True}
    assert set(type(current).model_fields) == {"measurements"}
    assert "unknown" not in current.__dict__


def test_entity_family_and_uuid_separate_keys():
    identity_map = IdentityMap()
    entity_id = uuid4()

    resource = identity_map.intern(_resource(entity_id))
    namespace = identity_map.intern(
        NamespaceSchema.model_construct(
            id=entity_id,
            create_date=datetime(2026, 1, 1),
            modified_date=datetime(2026, 1, 1),
            path="sample",
            parent_id=None,
            status="ACTIVE",
            revision=1,
            metadata={},
        )
    )

    assert resource is not namespace


def test_identity_map_keeps_namespace_context_outside_backend_entity_family():
    identity_map = IdentityMap()
    namespace_id = uuid4()
    context = NamespaceContext(id=namespace_id, path="beamline")
    namespace = NamespaceSchema.model_construct(
        id=namespace_id,
        create_date=datetime(2026, 1, 1),
        modified_date=datetime(2026, 1, 1),
        path="beamline",
        parent_id=None,
        status="ACTIVE",
        revision=1,
        metadata={},
    )

    canonical = identity_map.intern(namespace)

    assert canonical is not context
    assert identity_map.intern(context) is context


def test_higher_revision_merges_into_first_canonical_object():
    identity_map = IdentityMap()
    resource_id = uuid4()
    first = identity_map.intern(_resource(resource_id, name="old", revision=1))

    result = identity_map.intern(_resource(resource_id, name="new", revision=2))

    assert result is first
    assert first.name == "new"
    assert first.revision == 2


def test_lower_revision_does_not_overwrite_canonical_object():
    identity_map = IdentityMap()
    resource_id = uuid4()
    first = identity_map.intern(_resource(resource_id, name="new", revision=2))

    result = identity_map.intern(_resource(resource_id, name="old", revision=1))

    assert result is first
    assert first.name == "new"


def test_lower_revision_does_not_merge_loaded_relations():
    identity_map = IdentityMap()
    resource_id = uuid4()
    first = _resource(resource_id, revision=2)
    first.children = {"new": _resource(uuid4(), name="new")}
    first.set_loaded_relations({"children": True})
    canonical = identity_map.intern(first)
    new_child = canonical.children["new"]

    incoming = _resource(resource_id, revision=1)
    incoming.children = {"old": _resource(uuid4(), name="old")}
    incoming.set_loaded_relations({"children": True})
    identity_map.intern(incoming)

    assert canonical.children == {"new": new_child}


def test_equal_revision_conflicting_data_raises_merge_conflict():
    identity_map = IdentityMap()
    resource_id = uuid4()
    identity_map.intern(_resource(resource_id, name="first", revision=1))

    with pytest.raises(IdentityMergeConflict):
        identity_map.intern(_resource(resource_id, name="second", revision=1))


def test_equal_revision_conflicting_status_raises_merge_conflict():
    identity_map = IdentityMap()
    resource_id = uuid4()
    first = _resource(resource_id, revision=1)
    identity_map.intern(first)

    second = _resource(resource_id, revision=1)
    second.namespace_id = first.namespace_id
    second.status = "ARCHIVED"
    with pytest.raises(IdentityMergeConflict, match="status"):
        identity_map.intern(second)


def test_equal_revision_loaded_relation_conflict_raises_merge_conflict():
    identity_map = IdentityMap()
    resource_id = uuid4()
    first = _resource(resource_id)
    second_namespace_id = first.namespace_id
    first.children = [_resource(uuid4(), name="first")]
    first.set_loaded_relations({"children": True})
    identity_map.intern(first)

    second = _resource(resource_id)
    second.namespace_id = second_namespace_id
    second.children = [_resource(uuid4(), name="second")]
    second.set_loaded_relations({"children": True})
    with pytest.raises(IdentityMergeConflict):
        identity_map.intern(second)


def test_equal_revision_omitted_relation_containers_do_not_conflict():
    identity_map = IdentityMap()
    resource_id = uuid4()
    first = _resource(resource_id)
    second_namespace_id = first.namespace_id
    first.children = [_resource(uuid4(), name="first")]
    first.set_loaded_relations({"children": False})
    identity_map.intern(first)

    second = _resource(resource_id)
    second.namespace_id = second_namespace_id
    second.children = [_resource(uuid4(), name="second")]
    second.set_loaded_relations({})

    assert identity_map.intern(second) is first


def test_equal_revision_loaded_relation_conflict_is_deterministic():
    identity_map = IdentityMap()
    resource_id = uuid4()
    first = _resource(resource_id)
    second_namespace_id = first.namespace_id
    first.children = [_resource(uuid4(), name="first")]
    first.set_loaded_relations({"children": True})
    identity_map.intern(first)

    second = _resource(resource_id)
    second.namespace_id = second_namespace_id
    second.children = [_resource(uuid4(), name="second")]
    second.set_loaded_relations({"children": True})

    with pytest.raises(IdentityMergeConflict, match="children"):
        identity_map.intern(second)


def test_repeated_partial_and_full_hydration_does_not_conflict_on_omitted_relations():
    identity_map = IdentityMap()
    resource_id = uuid4()
    namespace_id = uuid4()
    first = _resource(resource_id)
    first.namespace_id = namespace_id
    first.children = [_resource(uuid4(), name="child")]
    first.set_loaded_relations({"children": True})
    canonical = identity_map.intern(first)

    partial = _resource(resource_id)
    partial.namespace_id = namespace_id
    partial.children = []
    partial.set_loaded_relations({})

    assert identity_map.intern(partial) is canonical
    assert len(canonical.children) == 1


def test_equal_revision_resource_template_projection_upgrade_does_not_conflict():
    identity_map = IdentityMap()
    resource_id = uuid4()
    template_id = uuid4()
    namespace_id = uuid4()
    full_template = ResourceTemplateSchema.model_construct(
        id=template_id,
        namespace_id=namespace_id,
        create_date=datetime(2026, 1, 1),
        modified_date=datetime(2026, 1, 1),
        status="ACTIVE",
        revision=1,
        name="Sample",
        slug="sample",
        version="1.0",
        labels=[],
        types=[],
        children={},
        attribute_group_templates=[],
    )
    full_template.set_loaded_relations(
        {
            "parent": False,
            "children": True,
            "types": True,
            "attribute_group_templates": True,
        }
    )
    first = _resource(resource_id)
    first.namespace_id = namespace_id
    first.template = full_template
    first.set_loaded_relations({"template": True})
    canonical = identity_map.intern(first)

    partial_template = ResourceTemplateSchema.model_construct(
        id=template_id,
        namespace_id=namespace_id,
        create_date=full_template.create_date,
        modified_date=full_template.modified_date,
        status="ACTIVE",
        revision=1,
        name="Sample",
        slug="sample",
        version="1.0",
    )
    partial_template.set_loaded_relations({"parent": False})
    second = _resource(resource_id)
    second.namespace_id = namespace_id
    second.template = partial_template
    second.set_loaded_relations({"template": True})

    assert identity_map.intern(second) is canonical
    assert canonical.template is full_template


def test_equal_revision_nested_loaded_scalar_conflict_is_deterministic():
    identity_map = IdentityMap()
    resource_id = uuid4()
    child_id = uuid4()
    namespace_id = uuid4()
    first = _resource(resource_id)
    first.namespace_id = namespace_id
    first.children = [_resource(child_id, name="first")]
    first.set_loaded_relations({"children": True})
    identity_map.intern(first)

    second = _resource(resource_id)
    second.namespace_id = namespace_id
    second.children = [_resource(child_id, name="second")]
    second.set_loaded_relations({"children": True})

    with pytest.raises(IdentityMergeConflict, match="children"):
        identity_map.intern(second)


def test_higher_revision_replaces_loaded_relation_container():
    identity_map = IdentityMap()
    resource_id = uuid4()
    first = _resource(resource_id)
    first.children = []
    first.set_loaded_relations({"children": True})
    canonical = identity_map.intern(first)
    old_children = canonical.children

    second = _resource(resource_id, revision=2)
    child = _resource(uuid4(), name="child")
    second.children = [child]
    second.set_loaded_relations({"children": True})
    identity_map.intern(second)

    assert canonical.children is not old_children
    assert canonical.children[0].id == child.id


def test_higher_revision_replaces_loaded_relation_container_to_reflect_deletions():
    identity_map = IdentityMap()
    resource_id = uuid4()
    retained = _resource(uuid4(), name="retained")
    deleted = _resource(uuid4(), name="deleted")
    first = _resource(resource_id)
    first.children = [retained, deleted]
    first.set_loaded_relations({"children": True})
    canonical = identity_map.intern(first)
    old_children = canonical.children

    incoming = _resource(resource_id, revision=2)
    incoming.children = [retained]
    incoming.set_loaded_relations({"children": True})
    identity_map.intern(incoming)

    assert canonical.children == [retained]
    assert canonical.children is not old_children


def test_authoritative_command_merge_does_not_change_later_query_merges():
    identity_map = IdentityMap()
    resource_id = uuid4()
    initial = _resource(resource_id)
    initial.children = [_resource(uuid4(), name="old")]
    initial.set_loaded_relations({"children": True})
    canonical = identity_map.intern(initial)

    command_result = _resource(resource_id, revision=2, name="command")
    command_result.children = []
    command_result.set_loaded_relations({"children": True})
    identity_map.intern(command_result, authoritative=True)

    query_result = _resource(resource_id, revision=3, name="query")
    query_child = _resource(uuid4(), name="query-child")
    query_result.children = [query_child]
    query_result.set_loaded_relations({"children": True})
    identity_map.intern(query_result)

    assert canonical.name == "query"
    assert canonical.children == [query_child]

    conflicting_query = _resource(resource_id, revision=3, name="conflict")
    conflicting_query.children = [query_child]
    conflicting_query.set_loaded_relations({"children": True})
    with pytest.raises(IdentityMergeConflict):
        identity_map.intern(conflicting_query)


def test_equal_revision_compatible_relation_loads_extend_without_deleting():
    identity_map = IdentityMap()
    resource_id = uuid4()
    first = _resource(resource_id)
    first.children = [_resource(uuid4(), name="first")]
    first.set_loaded_relations({"children": True})
    canonical = identity_map.intern(first)

    incoming = _resource(resource_id)
    incoming.namespace_id = first.namespace_id
    incoming.children = [first.children[0], _resource(uuid4(), name="second")]
    incoming.set_loaded_relations({"children": True})
    identity_map.intern(incoming)

    assert {child.name for child in canonical.children} == {"first", "second"}


def test_repeated_loaded_value_array_reload_does_not_duplicate_items():
    identity_map = IdentityMap()
    resource_id = uuid4()
    type_id = uuid4()
    template_id = uuid4()
    namespace_id = uuid4()
    first = _resource(resource_id)
    first.namespace_id = namespace_id
    first.template = ResourceTemplateSchema.model_construct(
        id=template_id,
        namespace_id=namespace_id,
        create_date=datetime(2026, 1, 1),
        modified_date=datetime(2026, 1, 1),
        status="ACTIVE",
        revision=1,
        name="Sample",
        slug="sample",
        version="1.0",
        types=[ResourceTypeSchema.model_construct(id=type_id, name="sample")],
    )
    first.template.set_loaded_relations({"types": True})
    first.set_loaded_relations({"template": True})
    canonical = identity_map.intern(first)

    second = _resource(resource_id)
    second.namespace_id = namespace_id
    second.template = ResourceTemplateSchema.model_construct(
        id=template_id,
        namespace_id=namespace_id,
        create_date=datetime(2026, 1, 1),
        modified_date=datetime(2026, 1, 1),
        status="ACTIVE",
        revision=1,
        name="Sample",
        slug="sample",
        version="1.0",
        types=[ResourceTypeSchema.model_construct(id=type_id, name="sample")],
    )
    second.template.set_loaded_relations({"types": True})
    second.set_loaded_relations({"template": True})
    identity_map.intern(second)

    assert [item.id for item in canonical.template.types] == [type_id]


@pytest.mark.parametrize(
    "model", [ParameterSchema, ResourceSlotSchema, ResourceTypeSchema]
)
def test_value_models_are_not_interned(model):
    identity_map = IdentityMap()
    model_id = uuid4()
    first = model.model_construct(id=model_id)
    second = model.model_construct(id=model_id)

    assert identity_map.intern(first) is first
    assert identity_map.intern(second) is second


def test_models_without_revision_merge_only_when_modified_date_is_newer():
    identity_map = IdentityMap()
    entity_id = uuid4()
    old = NamespaceSchema.model_construct(
        id=entity_id,
        create_date=datetime(2026, 1, 1),
        modified_date=datetime(2026, 1, 2),
        path="old",
        parent_id=None,
        status="ACTIVE",
        revision=None,
        metadata={},
    )
    newer = old.model_copy(
        update={"path": "new", "modified_date": datetime(2026, 1, 3)}
    )

    canonical = identity_map.intern(old)
    identity_map.intern(newer)

    assert canonical.path == "new"


def test_clear_removes_strong_references():
    identity_map = IdentityMap()
    resource_id = uuid4()
    identity_map.intern(_resource(resource_id))

    identity_map.clear()

    assert identity_map.get(("resource", resource_id)) is None


def test_connection_state_clears_identity_map_once_on_final_close():
    backend = ClientBackend.__new__(ClientBackend)
    object.__setattr__(backend, "_close_lock", RLock())
    object.__setattr__(backend, "_closed", False)
    object.__setattr__(backend, "identity_map", IdentityMap())
    for name in ("reader", "writer", "namespaces", "namespace_writer"):
        object.__setattr__(backend, name, object())
    state = ConnectionState(backend=backend)
    state.acquire()
    state.acquire()
    resource_id = uuid4()
    backend.identity_map.intern(_resource(resource_id))

    state.release()
    assert backend.identity_map.get(("resource", resource_id)) is not None
    state.release()
    state.close()

    assert backend.identity_map.get(("resource", resource_id)) is None


def test_direct_backend_close_clears_identity_map_idempotently():
    backend = ClientBackend.__new__(ClientBackend)
    object.__setattr__(backend, "_close_lock", RLock())
    object.__setattr__(backend, "_closed", False)
    object.__setattr__(backend, "identity_map", IdentityMap())
    for name in ("reader", "writer", "namespaces", "namespace_writer"):
        object.__setattr__(backend, name, object())
    resource_id = uuid4()
    backend.identity_map.intern(_resource(resource_id))

    backend.close()
    backend.close()

    assert backend.identity_map.get(("resource", resource_id)) is None


def test_concurrent_direct_backend_close_closes_capabilities_and_identity_once():
    class Closable:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    backend = ClientBackend.__new__(ClientBackend)
    object.__setattr__(backend, "_close_lock", RLock())
    object.__setattr__(backend, "_closed", False)
    identity_map = IdentityMap()
    object.__setattr__(backend, "identity_map", identity_map)
    capabilities = [Closable() for _ in range(4)]
    for name, capability in zip(
        ("reader", "writer", "namespaces", "namespace_writer"),
        capabilities,
        strict=True,
    ):
        object.__setattr__(backend, name, capability)
    resource_id = uuid4()
    identity_map.intern(_resource(resource_id))

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _index: backend.close(), range(32)))

    assert identity_map.get(("resource", resource_id)) is None
    assert [capability.close_calls for capability in capabilities] == [1] * 4


def test_direct_backend_close_closes_optional_capabilities_once_by_identity():
    class Closable:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    backend = ClientBackend.__new__(ClientBackend)
    object.__setattr__(backend, "_close_lock", RLock())
    object.__setattr__(backend, "_closed", False)
    object.__setattr__(backend, "identity_map", IdentityMap())
    capabilities = [Closable() for _ in range(6)]
    for name, capability in zip(
        (
            "reader",
            "writer",
            "namespaces",
            "namespace_writer",
            "context_resolver",
            "permissions",
        ),
        capabilities,
        strict=True,
    ):
        object.__setattr__(backend, name, capability)

    backend.close()
    backend.close()

    assert [capability.close_calls for capability in capabilities] == [1] * 6


def test_backend_close_retries_failed_capability_without_repeating_successful_cleanup():
    class FlakyIdentityMap(IdentityMap):
        def __init__(self):
            super().__init__()
            self.clear_calls = 0

        def clear(self):
            self.clear_calls += 1
            if self.clear_calls == 1:
                raise RuntimeError("clear failed")
            super().clear()

    class Closable:
        def __init__(self, *, failures=0):
            self.close_calls = 0
            self.failures = failures

        def close(self):
            self.close_calls += 1
            if self.failures:
                self.failures -= 1
                raise RuntimeError("close failed")

    backend = ClientBackend.__new__(ClientBackend)
    object.__setattr__(backend, "_close_lock", RLock())
    object.__setattr__(backend, "_closed", False)
    identity_map = FlakyIdentityMap()
    object.__setattr__(backend, "identity_map", identity_map)
    failing = Closable(failures=2)
    successful = Closable()
    for name, capability in zip(
        ("reader", "writer", "namespaces", "namespace_writer"),
        (failing, successful, successful, successful),
        strict=True,
    ):
        object.__setattr__(backend, name, capability)
    resource_id = uuid4()
    identity_map.intern(_resource(resource_id))

    with pytest.raises(RuntimeError, match="clear failed"):
        backend.close()

    assert identity_map.get(("resource", resource_id)) is not None
    assert failing.close_calls == 1
    assert successful.close_calls == 1

    with pytest.raises(RuntimeError, match="close failed"):
        backend.close()

    assert identity_map.get(("resource", resource_id)) is None
    assert identity_map.clear_calls == 2
    assert failing.close_calls == 2
    assert successful.close_calls == 1

    backend.close()

    assert identity_map.clear_calls == 2
    assert failing.close_calls == 3
    assert successful.close_calls == 1

    backend.close()
    assert failing.close_calls == 3


def test_recap_client_close_retries_failed_shared_cleanup(monkeypatch):
    class FlakyIdentityMap(IdentityMap):
        def __init__(self):
            super().__init__()
            self.clear_calls = 0

        def clear(self):
            self.clear_calls += 1
            if self.clear_calls == 1:
                raise RuntimeError("clear failed")
            super().clear()

    class _ContextResolver:
        def get_namespace_context(self, path=""):
            pass

    backend = ClientBackend.__new__(ClientBackend)
    object.__setattr__(backend, "_close_lock", RLock())
    object.__setattr__(backend, "_closed", False)
    object.__setattr__(backend, "identity_map", FlakyIdentityMap())
    object.__setattr__(backend, "context_resolver", _ContextResolver())
    monkeypatch.setattr(
        backend.context_resolver,
        "get_namespace_context",
        lambda _adapter, path="": NamespaceContext(
            id=UUID(int=0), path=path, metadata={}
        ),
    )
    for name in ("reader", "writer", "namespaces", "namespace_writer"):
        object.__setattr__(backend, name, object())

    client = RecapClient._from_backends(backend, namespace="")
    state = client.connection_state

    with pytest.raises(RuntimeError, match="clear failed"):
        client.close()

    assert client._closed is False
    assert client.connection_state is state
    assert state.closed is False

    client.close()

    assert client._closed is True
    assert client.connection_state is state
    assert state.closed is True
    assert backend.identity_map.clear_calls == 2


def test_root_and_scoped_clients_share_identity_map(tmp_path):
    root = RecapClient.from_sqlite(tmp_path / "identity.db")
    root.create_namespace("beamline")
    scoped = root.namespace("beamline")

    assert (
        scoped.connection_state.backend.identity_map
        is root.connection_state.backend.identity_map
    )

    scoped.close()
    root.close()


def test_final_root_close_clears_shared_identity_map_once(tmp_path):
    root = RecapClient.from_sqlite(tmp_path / "identity.db")
    root.create_namespace("beamline")
    scoped = root.namespace("beamline")

    with patch.object(
        root.connection_state.backend.identity_map,
        "clear",
        wraps=root.connection_state.backend.identity_map.clear,
    ) as clear:
        scoped.close()
        root.close()

    clear.assert_called_once_with()


def test_separate_backend_connections_have_separate_identity_maps():
    first = RecapClient.from_sqlite()
    second = RecapClient.from_sqlite()

    assert (
        first.connection_state.backend.identity_map
        is not second.connection_state.backend.identity_map
    )

    first.close()
    second.close()
