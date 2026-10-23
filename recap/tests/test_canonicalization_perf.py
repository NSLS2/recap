"""Flat materialization work scales with unique records, not shared subgraphs."""

from collections import Counter
from unittest.mock import patch

import pytest

from recap.adapter.entity_batch import EntityBatchCollector
from recap.client.identity import IdentityMap
from recap.client.materialization import BatchMaterializer
from recap.db.resource import Resource
from recap.schemas.resource import ResourceSchema

from .test_hydration_cache import _seed_resource_graph


def _plate_batch(db_session, count):
    parent, _, template, _ = _seed_resource_graph(db_session)
    for index in range(count):
        db_session.add(
            Resource(
                name=f"well-{index}",
                template=template,
                parent=parent,
                _init_children=False,
            )
        )
    db_session.flush()
    collector = EntityBatchCollector()
    collector.add_root(parent, {"template", "properties", "children"})
    return collector.collect()


@pytest.mark.performance
@pytest.mark.parametrize("well_count", [12, 96])
def test_shared_template_materialization_validates_each_record_once(
    db_session, well_count
):
    batch = _plate_batch(db_session, well_count)
    materializer = BatchMaterializer(IdentityMap())
    visits = Counter()
    validate = materializer._fields

    def counted(record):
        visits[record.kind, record.id] += 1
        return validate(record)

    with patch.object(materializer, "_fields", side_effect=counted):
        model = materializer.materialize(ResourceSchema, batch)[0]
    assert len(visits) == len(batch.entities)
    assert set(visits.values()) == {1}
    assert all(
        model.children[f"well-{index}"].template is model.template
        for index in range(well_count)
    )


@pytest.mark.performance
def test_repeated_plate_materialization_has_linear_record_budget(db_session):
    batch = _plate_batch(db_session, 96)
    materializer = BatchMaterializer(IdentityMap())
    first = materializer.materialize(ResourceSchema, batch)[0]
    with patch.object(materializer, "_fields", wraps=materializer._fields) as fields:
        for _ in range(3):
            assert materializer.materialize(ResourceSchema, batch)[0] is first
    assert fields.call_count == 3 * len(batch.entities)
