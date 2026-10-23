from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from recap.adapter.entity_records import EntityBatch
from recap.commands.context import build_local_command_context
from recap.commands.errors import CommandValidationError
from recap.commands.models import (
    CreateProcessRun,
    CreateResource,
    SetLifecycleStatus,
    UpdateProcessRun,
    UpdateResource,
)
from recap.commands.service import CommandService
from recap.db.idempotency import IdempotencyRecord
from recap.db.resource import Resource
from recap.dsl.drafts import ProcessRunDraft
from recap.schemas.process import ProcessRunSchema
from recap.schemas.resource import ResourceSchema

from .entity_fixtures import _seed_resource_graph
from .test_process_run_resource_tree_perf import _seed_run_with_resource_chain


def test_command_batch_replay_preserves_original_snapshot(db_session):
    parent, _, template, _ = _seed_resource_graph(db_session)
    path, template_id = parent.namespace.path, template.id
    db_session.commit()
    service = CommandService(sessionmaker(bind=db_session.get_bind()))
    context = build_local_command_context()
    context = replace(context, idempotency_key=str(uuid4()))
    command = CreateResource(
        namespace_path=path, name="batch-created", template_id=template_id
    )
    first = service.execute_batch(command, context)
    assert isinstance(first, EntityBatch)
    service.execute_batch(
        UpdateResource(
            resource_id=first.roots[0].id,
            expected_revision=1,
            name="changed-after-create",
        ),
        replace(context, idempotency_key=str(uuid4())),
    )
    replay = service.execute_batch(command, context)
    assert first.model_dump(mode="json") == replay.model_dump(mode="json")
    assert len(first.roots) == 1
    assert any(record.kind == "property" for record in first.entities)


def test_command_batch_api_does_not_construct_nested_resource_graph(db_session):
    from unittest.mock import patch

    from recap.schemas.resource import ResourceSchema

    parent, _, template, _ = _seed_resource_graph(db_session)
    path, template_id = parent.namespace.path, template.id
    db_session.commit()
    service = CommandService(sessionmaker(bind=db_session.get_bind()))
    with patch.object(
        ResourceSchema,
        "model_validate",
        side_effect=AssertionError("Nested ORM hydration"),
    ):
        result = service.execute_batch(
            CreateResource(
                namespace_path=path, name="flat-command", template_id=template_id
            ),
            build_local_command_context(),
        )
    assert result.roots[0].kind == "resource"


@pytest.mark.parametrize("entry", ["execute", "execute_batch", "create_resource"])
def test_all_service_entry_points_persist_flat_snapshots(db_session, entry):
    parent, _, template, _ = _seed_resource_graph(db_session)
    path, template_id = parent.namespace.path, template.id
    db_session.commit()
    service = CommandService(sessionmaker(bind=db_session.get_bind()))
    context = replace(build_local_command_context(), idempotency_key=str(uuid4()))
    command = CreateResource(
        namespace_path=path, name="unified-result", template_id=template_id
    )
    with patch.object(
        ResourceSchema,
        "model_validate",
        side_effect=AssertionError("Nested ORM hydration"),
    ):
        if entry == "create_resource":
            result = service.create_resource(context, **command.model_dump())
        else:
            result = getattr(service, entry)(command, context)
    assert isinstance(
        result, EntityBatch if entry == "execute_batch" else ResourceSchema
    )
    record = db_session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.idempotency_key == context.idempotency_key
        )
    )
    assert record.response_json["entity"] == "resource"
    assert len(record.response_json["roots"]) == 1
    first_payload = record.response_json
    replay = service.execute_batch(command, context)
    assert isinstance(replay, EntityBatch)
    assert replay.roots[0].id == (
        result.roots[0].id if isinstance(result, EntityBatch) else result.id
    )
    assert first_payload["entities"]


def test_process_run_direct_execution_and_replay_use_flat_snapshots(db_session):
    seeded = _seed_run_with_resource_chain(db_session, name="command-run", depth=2)
    path, template_id = seeded.namespace.path, seeded.process_template_id
    service = CommandService(sessionmaker(bind=db_session.get_bind()))
    context = replace(build_local_command_context(), idempotency_key=str(uuid4()))
    command = CreateProcessRun(
        namespace_path=path,
        draft=ProcessRunDraft(
            name="direct-run", description="snapshot", template_id=template_id
        ),
    )
    with patch.object(
        ProcessRunSchema,
        "model_validate",
        side_effect=AssertionError("Nested process hydration"),
    ):
        first = service.create_process_run(
            context, namespace_path=path, draft=command.draft
        )
        assert isinstance(first, ProcessRunSchema)
        snapshot = first.model_dump(mode="json")
        service.execute(
            UpdateProcessRun(
                process_run_id=first.id, expected_revision=1, description="later"
            ),
            replace(context, idempotency_key=str(uuid4())),
        )
        replay = service.execute(command, context)
    assert replay.model_dump(mode="json") == snapshot
    assert len(list(next(iter(replay.steps.values())).parameters.values())) == 1


@pytest.mark.parametrize(
    "kind", ["resource", "resource_template", "process_template", "process_run"]
)
def test_lifecycle_execution_and_replay_have_typed_single_batch_root(db_session, kind):
    run = _seed_run_with_resource_chain(db_session, name="lifecycle-batch", depth=1)
    resource = next(iter(run.resources.values()))
    rows = {
        "resource": resource,
        "resource_template": resource.template,
        "process_template": run.template,
        "process_run": run,
    }
    row = rows[kind]
    object_id, revision = row.id, row.revision
    service = CommandService(sessionmaker(bind=db_session.get_bind()))
    context = replace(build_local_command_context(), idempotency_key=str(uuid4()))
    command = SetLifecycleStatus(
        object_type=kind,
        object_id=object_id,
        expected_revision=revision,
        status="ARCHIVED",
    )
    result = service.execute_batch(command, context)
    assert len(result.roots) == 1
    assert result.roots[0].kind == kind
    replay = service.execute(command, context)
    assert replay.id == object_id
    assert replay.revision == revision + 1
    assert replay.status.value == "ARCHIVED"


def test_result_collection_failure_rolls_back_write_and_replay_claim(
    blank_database_path, copy_database, tmp_path
):
    database = copy_database(blank_database_path, tmp_path / "rollback.db")
    engine = create_engine(f"sqlite:///{database}")
    factory = sessionmaker(bind=engine)
    with factory.begin() as session:
        parent, _, template, _ = _seed_resource_graph(session)
        path, template_id = parent.namespace.path, template.id
    service = CommandService(factory)
    context = replace(build_local_command_context(), idempotency_key=str(uuid4()))
    command = CreateResource(
        namespace_path=path, name="rolled-back-result", template_id=template_id
    )
    with (
        patch.object(
            service,
            "_graph_result",
            side_effect=CommandValidationError("Invalid result"),
        ),
        pytest.raises(CommandValidationError, match="Invalid result"),
    ):
        service.execute(command, context)
    with factory() as session:
        assert (
            session.scalar(select(Resource).where(Resource.name == command.name))
            is None
        )
        assert (
            session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.idempotency_key == context.idempotency_key
                )
            )
            is None
        )
    created = service.execute(command, context)
    assert created.name == command.name
    engine.dispose()
