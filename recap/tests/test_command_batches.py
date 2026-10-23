from dataclasses import replace
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from recap.adapter.entity_records import EntityBatch
from recap.commands.context import build_local_command_context
from recap.commands.models import CreateResource, UpdateResource
from recap.commands.service import CommandService

from .test_hydration_cache import _seed_resource_graph


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
