"""Performance regression tests for client-side canonicalization.

The client keeps a single canonical copy of a repeated object (e.g. one well
template shared by all 96 wells of a plate). Deduplication saves *memory*, but
the CPU cost of *reaching* that dedup must not scale with the number of
occurrences: interning a plate whose 96 wells share one template must walk that
template's subtree a bounded number of times, not 96 times.

The regression these tests guard against is re-canonicalizing (and re-merging)
an already-canonical subtree once per parent that references it -- an
O(occurrences x subtree) blow-up in ``IdentityMap._canonicalize_value`` /
``_canonicalize_relations``.

The assertion is *occurrence-independence*: a plate with 96 shared-template
wells and a plate with 12 must issue a comparable (bounded) number of
canonicalization visits, dominated by the unique object count, not the well
count.
"""

from datetime import datetime
from uuid import uuid4

import pytest

from recap.client.identity import IdentityMap
from recap.schemas.attribute import (
    AttributeGroupTemplateSchema,
    AttributeTemplateSchema,
)
from recap.schemas.resource import ResourceSchema, ResourceTemplateSchema

_NS = uuid4()


def _attr_template(name: str) -> AttributeTemplateSchema:
    return AttributeTemplateSchema.model_construct(
        id=uuid4(),
        create_date=datetime(2026, 1, 1),
        modified_date=datetime(2026, 1, 1),
        namespace_id=_NS,
        status="ACTIVE",
        revision=1,
        name=name,
        slug=name,
        value_type="int",
        default_value="0",
    )


def _make_well_template() -> ResourceTemplateSchema:
    """A single well template with a few attribute groups/templates."""
    groups = []
    for g in range(3):
        grp = AttributeGroupTemplateSchema.model_construct(
            id=uuid4(),
            create_date=datetime(2026, 1, 1),
            modified_date=datetime(2026, 1, 1),
            namespace_id=_NS,
            status="ACTIVE",
            revision=1,
            name=f"group{g}",
            slug=f"group{g}",
            attribute_templates=[_attr_template(f"a{g}_{j}") for j in range(4)],
        )
        grp.set_loaded_relations({"attribute_templates": True})
        groups.append(grp)
    tmpl = ResourceTemplateSchema.model_construct(
        id=uuid4(),
        create_date=datetime(2026, 1, 1),
        modified_date=datetime(2026, 1, 1),
        namespace_id=_NS,
        status="ACTIVE",
        revision=1,
        name="well",
        slug="well",
        version="1",
        labels=[],
        types=[],
        children={},
        attribute_group_templates=groups,
    )
    tmpl.set_loaded_relations(
        {
            "parent": True,
            "children": True,
            "types": True,
            "attribute_group_templates": True,
        }
    )
    return tmpl


def _well(i: int, template: ResourceTemplateSchema) -> ResourceSchema:
    w = ResourceSchema.model_construct(
        id=uuid4(),
        create_date=datetime(2026, 1, 1),
        modified_date=datetime(2026, 1, 1),
        namespace_id=_NS,
        status="ACTIVE",
        revision=1,
        name=f"well-{i:02d}",
        template=template,
        children={},
        properties={},
    )
    w.set_loaded_relations(
        {"template": True, "parent": True, "children": True, "properties": True}
    )
    return w


def _make_plate(well_count: int) -> ResourceSchema:
    """A plate whose wells all share one canonical well template."""
    template = _make_well_template()
    wells = {
        f"well-{i:02d}": _well(i, template) for i in range(well_count)
    }
    plate = ResourceSchema.model_construct(
        id=uuid4(),
        create_date=datetime(2026, 1, 1),
        modified_date=datetime(2026, 1, 1),
        namespace_id=_NS,
        status="ACTIVE",
        revision=1,
        name="plate",
        template=template,
        children=wells,
        properties={},
    )
    plate.set_loaded_relations(
        {"template": True, "parent": True, "children": True, "properties": True}
    )
    return plate


def _count_canonicalize_calls(plate: ResourceSchema) -> int:
    """Intern ``plate`` and count ``_canonicalize_value`` invocations."""
    identity_map = IdentityMap()
    calls = 0
    original = IdentityMap._canonicalize_value

    def counting(self, value):  # noqa: ANN001
        nonlocal calls
        calls += 1
        return original(self, value)

    IdentityMap._canonicalize_value = counting
    try:
        identity_map.intern(plate)
    finally:
        IdentityMap._canonicalize_value = original
    return calls


@pytest.mark.performance
def test_shared_template_canonicalization_is_occurrence_independent():
    """Interning a 96-well plate must not re-walk the shared template per well.

    Both plates share one canonical well template. The number of
    ``_canonicalize_value`` visits must be dominated by the (fixed) unique
    object count -- growing at most linearly with the well *entries* being
    interned, never multiplicatively with the shared template subtree.
    """
    small = _count_canonicalize_calls(_make_plate(12))
    large = _count_canonicalize_calls(_make_plate(96))

    # Per-well overhead is bounded and small (each well contributes a constant
    # number of visits: itself + its template pointer, deduped on lookup).
    # Guard against the O(occurrences x subtree) regression where each of the
    # extra 84 wells re-walks the ~15-node template subtree.
    extra_wells = 96 - 12
    per_well_budget = 8  # generous; regression pushes this into the hundreds
    assert large - small <= extra_wells * per_well_budget, (
        f"canonicalization scales with occurrences: 12-well={small}, "
        f"96-well={large} _canonicalize_value calls "
        f"({(large - small) / extra_wells:.1f} per extra well)"
    )


@pytest.mark.performance
def test_shared_template_canonicalization_absolute_bound():
    """The absolute visit count for a 96-well plate stays a small multiple of
    the object count, not O(wells x template-size)."""
    plate = _make_plate(96)
    calls = _count_canonicalize_calls(plate)

    # Unique objects: 1 plate + 96 wells + 1 template + 3 groups + 12 attrs = 113.
    # A healthy canonicalization visits each field a bounded number of times.
    # The pre-fix path visited ~1.9M times for this payload.
    assert calls <= 3000, f"expected bounded canonicalization, got {calls} visits"


# --- Re-query path (item A): equal-revision merge signature hashing ----------
#
# Querying the same plate a second time interns a *fresh* object graph carrying
# the *same* ids and revision. The equal-revision merge path compares the
# incoming graph against the canonical one via ``_relation_signature``. Without
# a signature cache the whole graph is re-serialized on every re-query, and the
# shared template subtree is re-hashed once per well -- O(occurrences x subtree)
# on every poll of an already-cached object.
#
# These builders reuse fixed ids so re-interning maps onto the same canonical
# identities (unlike ``_make_plate``, which mints new ids each call).


def _fixed_plate_builder(well_count: int):
    """Return a zero-arg factory producing fresh graphs with stable ids."""
    plate_id = uuid4()
    template_id = uuid4()
    group_ids = [uuid4() for _ in range(3)]
    attr_ids = [[uuid4() for _ in range(4)] for _ in range(3)]
    well_ids = [uuid4() for _ in range(well_count)]

    def build_template() -> ResourceTemplateSchema:
        groups = []
        for g in range(3):
            grp = AttributeGroupTemplateSchema.model_construct(
                id=group_ids[g],
                create_date=datetime(2026, 1, 1),
                modified_date=datetime(2026, 1, 1),
                namespace_id=_NS,
                status="ACTIVE",
                revision=1,
                name=f"group{g}",
                slug=f"group{g}",
                attribute_templates=[
                    AttributeTemplateSchema.model_construct(
                        id=attr_ids[g][j],
                        create_date=datetime(2026, 1, 1),
                        modified_date=datetime(2026, 1, 1),
                        namespace_id=_NS,
                        status="ACTIVE",
                        revision=1,
                        name=f"a{g}_{j}",
                        slug=f"a{g}_{j}",
                        value_type="int",
                        default_value="0",
                    )
                    for j in range(4)
                ],
            )
            grp.set_loaded_relations({"attribute_templates": True})
            groups.append(grp)
        tmpl = ResourceTemplateSchema.model_construct(
            id=template_id,
            create_date=datetime(2026, 1, 1),
            modified_date=datetime(2026, 1, 1),
            namespace_id=_NS,
            status="ACTIVE",
            revision=1,
            name="well",
            slug="well",
            version="1",
            labels=[],
            types=[],
            children={},
            attribute_group_templates=groups,
        )
        tmpl.set_loaded_relations(
            {
                "parent": True,
                "children": True,
                "types": True,
                "attribute_group_templates": True,
            }
        )
        return tmpl

    def build() -> ResourceSchema:
        template = build_template()
        wells = {}
        for i in range(well_count):
            w = ResourceSchema.model_construct(
                id=well_ids[i],
                create_date=datetime(2026, 1, 1),
                modified_date=datetime(2026, 1, 1),
                namespace_id=_NS,
                status="ACTIVE",
                revision=1,
                name=f"well-{i:02d}",
                template=template,
                children={},
                properties={},
            )
            w.set_loaded_relations(
                {"template": True, "parent": True, "children": True, "properties": True}
            )
            wells[f"well-{i:02d}"] = w
        plate = ResourceSchema.model_construct(
            id=plate_id,
            create_date=datetime(2026, 1, 1),
            modified_date=datetime(2026, 1, 1),
            namespace_id=_NS,
            status="ACTIVE",
            revision=1,
            name="plate",
            template=template,
            children=wells,
            properties={},
        )
        plate.set_loaded_relations(
            {"template": True, "parent": True, "children": True, "properties": True}
        )
        return plate

    return build


def _count_requery_signature_calls(well_count: int, requeries: int) -> int:
    """Intern once, then re-intern ``requeries`` fresh same-id graphs.

    Returns total ``_relation_signature`` invocations across the re-queries
    (the initial intern is excluded).
    """
    build = _fixed_plate_builder(well_count)
    identity_map = IdentityMap()
    identity_map.intern(build())  # establish canonical graph

    calls = 0
    original = IdentityMap._relation_signature_inner

    def counting(self, value, _seen, _cycle_hits, _memo):  # noqa: ANN001
        nonlocal calls
        calls += 1
        return original(self, value, _seen, _cycle_hits, _memo)

    IdentityMap._relation_signature_inner = counting
    try:
        for _ in range(requeries):
            identity_map.intern(build())
    finally:
        IdentityMap._relation_signature_inner = original
    return calls


@pytest.mark.performance
def test_requery_signature_hashing_is_occurrence_independent():
    """Re-querying a plate must not re-hash the shared template per well.

    The signature cache collapses the canonical side's shared subtree to a
    single cached lookup, so the per-re-query signature work grows with the
    number of *distinct* incoming objects, not with wells x template-size.
    """
    requeries = 10
    small = _count_requery_signature_calls(12, requeries)
    large = _count_requery_signature_calls(96, requeries)

    extra_wells = 96 - 12
    # Each extra well adds a *bounded constant* of signature visits per
    # re-query -- its own node plus its handful of scalar fields, with the
    # shared template collapsed to a single memo hit. The regression re-hashed
    # the whole ~15-node template subtree per well per re-query (~135/well).
    # The healthy floor is ~12/well (one visit per unique incoming node);
    # 30 leaves generous headroom while still catching subtree re-hashing.
    per_well_per_requery_budget = 30
    budget = extra_wells * requeries * per_well_per_requery_budget
    assert large - small <= budget, (
        f"re-query signature hashing scales with occurrences: "
        f"12-well={small}, 96-well={large} _relation_signature calls over "
        f"{requeries} re-queries "
        f"({(large - small) / (extra_wells * requeries):.1f} per well per re-query)"
    )

