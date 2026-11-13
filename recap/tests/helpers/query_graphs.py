"""Query graph helpers and their narrowly imported namespace fixture."""

from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from recap.adapter.local import LocalBackend
from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.db.namespace import Namespace
from recap.db.process import ProcessRun, ProcessTemplate, ResourceSlot
from recap.db.resource import Resource, ResourceTemplate, ResourceType
from recap.db.step import StepTemplate, StepTemplateResourceSlotBinding
from recap.dsl.query import QueryDSL
from recap.lifecycle import LifecycleStatus
from recap.schemas.namespace import NamespaceContext
from recap.utils.general import Direction

_ACTIVE_CONTEXT = None


@pytest.fixture(autouse=True)
def active_namespace(db_session):
    global _ACTIVE_CONTEXT
    namespace = Namespace(
        id=uuid4(),
        path=f"test/{uuid4().hex}",
        metadata_json={},
        status=LifecycleStatus.ACTIVE,
    )
    db_session.add(namespace)
    db_session.flush()
    _ACTIVE_CONTEXT = NamespaceContext(id=namespace.id, path=namespace.path)

    def assign_namespace(_session, _flush_context, _instances):
        for item in _session.new:
            if isinstance(
                item, ProcessTemplate | ResourceTemplate | Resource | ProcessRun
            ):
                item.namespace = namespace
                item.status = LifecycleStatus.ACTIVE

    event.listen(db_session, "before_flush", assign_namespace)
    yield namespace
    event.remove(db_session, "before_flush", assign_namespace)


def make_query(db_session):
    SessionLocal = sessionmaker(bind=db_session.get_bind())
    backend = LocalBackend(SessionLocal)
    return QueryDSL(backend, context=_ACTIVE_CONTEXT)


def seed_process_run(
    db_session,
    *,
    name: str,
    with_parameters: bool = False,
    with_resource: bool = False,
    bind_step_slot: bool = False,
    with_resource_child: bool = False,
) -> tuple[Namespace, ProcessRun]:
    with db_session.no_autoflush:
        template = ProcessTemplate(name=f"Template-{name}", version="1.0")
        step_template = StepTemplate(name=f"Step-{name}", process_template=template)

        if with_parameters:
            attr = AttributeGroupTemplate(name=f"Exposure-{name}")
            attr.attribute_templates.append(
                AttributeTemplate(
                    name="dwell_time", value_type="int", default_value="5"
                )
            )
            step_template.attribute_group_templates.append(attr)
        run = ProcessRun(
            name=f"Run-{name}",
            description=f"Process run for {name}",
            template=template,
        )

    db_session.add_all([template, step_template])

    if with_resource:
        resource_type = ResourceType(name=f"resource-type-{uuid4().hex}")
        resource_template = ResourceTemplate(name=f"resource-template-{uuid4().hex}")
        resource_template.types.append(resource_type)
        slot = ResourceSlot(
            name=f"slot-{name}",
            process_template=template,
            resource_type=resource_type,
            direction=Direction.input,
        )
        resource = Resource(name=f"Resource-{name}", template=resource_template)
        if with_resource_child:
            Resource(
                name=f"Resource-{name}-child",
                template=resource_template,
                parent=resource,
            )
        db_session.add_all([resource_type, resource_template, slot, resource])
        if bind_step_slot:
            step_template.bindings["input_resource"] = StepTemplateResourceSlotBinding(
                role="input_resource",
                resource_slot=slot,
            )
    db_session.flush()
    db_session.add(run)
    if with_resource:
        run.resources[slot] = resource

    db_session.commit()
    return run.namespace, run
