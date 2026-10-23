import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from recap.adapter.query_loaders import (
    PRELOAD_STATEMENTS,
    resolve_loader_options,
)
from recap.db.attribute import AttributeValue
from recap.db.process import ProcessRun
from recap.db.resource import Property, Resource
from recap.db.step import Parameter
from recap.schemas.process import ProcessRunSchema
from recap.schemas.resource import ResourceSchema
from recap.utils.loaders import chain_load
from scripts.profile_bulk_loading import capture_sql

from .entity_fixtures import _seed_process_template, _seed_resource_graph


def test_duplicate_preloads_are_resolved_once_in_request_order(monkeypatch):
    calls = []

    def preload_options(schema, name):
        calls.append(name)
        return [name]

    monkeypatch.setattr("recap.adapter.query_loaders.preload_options", preload_options)

    options = resolve_loader_options(
        ProcessRunSchema, ["steps", "steps.parameters", "steps"], None
    )

    assert calls == ["steps", "steps.parameters"]
    assert options[-2:] == ["steps", "steps.parameters"]


def test_eager_mode_expands_stable_declared_paths(monkeypatch):
    calls = []

    def preload_options(schema, name):
        calls.append(name)
        return [name]

    monkeypatch.setattr("recap.adapter.query_loaders.preload_options", preload_options)

    resolve_loader_options(ResourceSchema, [], "eager")

    assert calls == ["template", "properties", "children"]


def test_unsupported_preload_is_validation_error():
    with pytest.raises(ValueError, match="Unsupported preload 'parameters'"):
        resolve_loader_options(ResourceSchema, ["parameters"], None)


def test_every_declared_preload_has_loader_statements():
    for (schema, relation), statements in PRELOAD_STATEMENTS.items():
        assert statements, (schema, relation)


@pytest.mark.parametrize(
    "preloads,mode", [(["properties"], None), (["properties"], "eager")]
)
def test_property_collections_arrive_with_key_templates(db_session, preloads, mode):
    parent, _, _, _ = _seed_resource_graph(db_session)
    parent_id = parent.id
    db_session.commit()
    engine = db_session.get_bind().engine
    with Session(db_session.get_bind()) as session, capture_sql(engine) as profile:
        row = session.scalar(
            select(Resource)
            .where(Resource.id == parent_id)
            .options(*resolve_loader_options(ResourceSchema, preloads, mode))
        )
        assert row.properties["Measurements"]._values["dose"].value == 1
    assert not profile.lazy_relationships, profile.lazy_relationships


@pytest.mark.parametrize("owner", [Property, Parameter], ids=["property", "parameter"])
@pytest.mark.parametrize("join_key", [False, True], ids=["select-in-key", "joined-key"])
def test_value_collection_scalar_joins_prevent_key_lazy_loads(
    db_session, owner, join_key
):
    if owner is Property:
        parent, _, _, _ = _seed_resource_graph(db_session)
        value_owner = parent.properties["Measurements"]
    else:
        template, _ = _seed_process_template(db_session)
        run = ProcessRun(
            name="value-loader-run",
            description="Cold values",
            template=template,
            namespace=template.namespace,
        )
        db_session.add(run)
        db_session.flush()
        value_owner = run.steps["Collect"].parameters["Exposure"]
    owner_id = value_owner.id
    db_session.commit()
    engine = db_session.get_bind().engine
    with Session(db_session.get_bind()) as session, capture_sql(engine) as profile:
        row = session.scalar(
            select(owner)
            .where(owner.id == owner_id)
            .options(
                chain_load(
                    owner._values,
                    AttributeValue.template,
                    joined=(AttributeValue.template,) if join_key else (),
                )
            )
        )
        assert len(row._values) == 1
        assert next(iter(row._values.values())).value == 1
    assert profile.loaded_types["AttributeValue"] == 1
    assert bool(profile.lazy_relationships) is not join_key


@pytest.mark.parametrize(
    "preloads,mode",
    [
        (["steps"], None),
        (["steps.parameters"], None),
        (["steps", "steps.parameters"], "eager"),
    ],
)
def test_parameter_collections_arrive_with_key_templates(db_session, preloads, mode):
    template, _ = _seed_process_template(db_session)
    run = ProcessRun(
        name="loader-run",
        description="Cold parameter loading",
        template=template,
        namespace=template.namespace,
    )
    db_session.add(run)
    db_session.flush()
    run_id = run.id
    db_session.commit()
    engine = db_session.get_bind().engine
    with Session(db_session.get_bind()) as session, capture_sql(engine) as profile:
        row = session.scalar(
            select(ProcessRun)
            .where(ProcessRun.id == run_id)
            .options(*resolve_loader_options(ProcessRunSchema, preloads, mode))
        )
        assert row.steps["Collect"].parameters["Exposure"].template.name == "Exposure"
        if "steps.parameters" in preloads:
            assert (
                row.steps["Collect"].parameters["Exposure"]._values["dwell"].value == 1
            )
    assert not profile.lazy_relationships, profile.lazy_relationships
