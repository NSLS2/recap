from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from sqlalchemy.orm import sessionmaker

from recap.adapter.local import LocalBackend
from recap.db.namespace import Namespace
from recap.db.process import ProcessRun, ProcessTemplate
from recap.db.resource import Resource, ResourceTemplate
from recap.lifecycle import LifecycleStatus
from recap.schemas.process import (
    ProcessRunSchema,
    ProcessTemplateRef,
    ProcessTemplateSchema,
)
from recap.schemas.resource import (
    ResourceSchema,
    ResourceTemplateRef,
    ResourceTemplateSchema,
)


def _namespace(path: str) -> Namespace:
    return Namespace(id=uuid4(), path=path, metadata_json={})


def test_template_identity_is_scoped_by_namespace(db_session):
    amx = _namespace("beamline/amx")
    fmx = _namespace("beamline/fmx")
    amx_template = ProcessTemplate(namespace=amx, name="data acquisition", version="1")
    fmx_template = ProcessTemplate(namespace=fmx, name="data acquisition", version="1")
    amx_resource_template = ResourceTemplate(
        namespace=amx, name="detector", version="1"
    )
    fmx_resource_template = ResourceTemplate(
        namespace=fmx, name="detector", version="1"
    )
    db_session.add_all(
        [
            amx,
            fmx,
            amx_template,
            fmx_template,
            amx_resource_template,
            fmx_resource_template,
        ]
    )
    db_session.flush()

    assert amx_template.name == fmx_template.name
    assert amx_template.namespace_id != fmx_template.namespace_id
    assert amx_resource_template.name == fmx_resource_template.name
    assert amx_resource_template.namespace_id != fmx_resource_template.namespace_id

    db_session.add(ProcessTemplate(namespace=amx, name="data acquisition", version="1"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_process_run_names_are_scoped_by_namespace(db_session):
    amx = _namespace("run/amx")
    fmx = _namespace("run/fmx")
    template_a = ProcessTemplate(namespace=amx, name="collect", version="1")
    template_b = ProcessTemplate(namespace=fmx, name="collect", version="1")
    run_a = ProcessRun(
        namespace=amx,
        name="run-1",
        description="",
        template=template_a,
    )
    run_b = ProcessRun(
        namespace=fmx,
        name="run-1",
        description="",
        template=template_b,
    )
    db_session.add_all([amx, fmx, template_a, template_b, run_a, run_b])
    db_session.flush()

    assert run_a.name == run_b.name
    assert run_a.namespace_id != run_b.namespace_id
    assert run_a.namespace is amx

    run_a.namespace_id = fmx.id
    with pytest.raises(ValueError, match="namespace_id is immutable"):
        db_session.flush()


def test_resource_top_level_identity_is_uuid_and_children_inherit_namespace(db_session):
    amx = _namespace("resource/amx")
    child_template = ResourceTemplate(name="well", version="1")
    template = ResourceTemplate(
        namespace=amx, name="plate", version="1", children={"well": child_template}
    )
    first = Resource(namespace=amx, name="plate-1", template=template)
    second = Resource(namespace=amx, name="plate-1", template=template)
    db_session.add_all([amx, template, first, second])
    db_session.flush()

    assert first.id != second.id
    assert child_template.namespace_id == amx.id
    assert first.children["well"].namespace_id == amx.id


@pytest.mark.parametrize(
    "factory",
    [
        lambda namespace: ProcessTemplate(
            namespace=namespace, name="process", version="1"
        ),
        lambda namespace: ResourceTemplate(
            namespace=namespace, name="resource template", version="1"
        ),
        lambda namespace: Resource(
            namespace=namespace,
            name="resource",
            template=ResourceTemplate(
                namespace=namespace, name="resource type", version="1"
            ),
        ),
    ],
)
def test_namespace_id_is_immutable(db_session, factory):
    namespace = _namespace(f"immutable/{uuid4()}")
    aggregate = factory(namespace)
    db_session.add_all([namespace, aggregate])
    db_session.flush()

    aggregate.namespace_id = uuid4()
    with pytest.raises(ValueError, match="namespace_id is immutable"):
        db_session.flush()


def test_template_labels_are_normalized(db_session):
    namespace = _namespace("labels")
    process_template = ProcessTemplate(
        namespace=namespace,
        name="collect",
        version="1",
        labels=["MX Data Acquisition", "FAST-PATH"],
    )
    resource_template = ResourceTemplate(
        namespace=namespace,
        name="sample",
        version="1",
        labels=["MX Sample Holder"],
    )
    db_session.add_all([namespace, process_template, resource_template])
    db_session.flush()

    assert process_template.labels == ["mx_data_acquisition", "fast_path"]
    assert resource_template.labels == ["mx_sample_holder"]

    process_data = ProcessTemplateRef.model_validate(process_template).model_dump()
    process_data["labels"] = ["MX Data Acquisition"]
    resource_data = ResourceTemplateRef.model_validate(resource_template).model_dump()
    resource_data["labels"] = ["MX Sample Holder"]
    assert ProcessTemplateRef.model_validate(process_data).labels == [
        "mx_data_acquisition"
    ]
    assert ResourceTemplateRef.model_validate(resource_data).labels == [
        "mx_sample_holder"
    ]


def test_aggregate_schemas_expose_namespace_lifecycle_and_copy_identity(db_session):
    namespace = _namespace("schemas")
    process_template = ProcessTemplate(
        namespace=namespace, name="collect", version="1", labels=["MX Collect"]
    )
    process_run = ProcessRun(
        namespace=namespace,
        name="run",
        description="",
        template=process_template,
    )
    resource_template = ResourceTemplate(
        namespace=namespace, name="sample", version="1", labels=["MX Sample"]
    )
    source = Resource(namespace=namespace, name="source", template=resource_template)
    copy = Resource(
        namespace=namespace,
        name="copy",
        template=resource_template,
        copied_from=source,
    )
    db_session.add_all(
        [
            namespace,
            process_template,
            process_run,
            resource_template,
            source,
            copy,
        ]
    )
    db_session.flush()

    schemas = (
        ProcessTemplateSchema.model_validate(process_template),
        ProcessRunSchema.model_validate(process_run),
        ResourceTemplateSchema.model_validate(resource_template),
        ResourceSchema.model_validate(copy),
    )
    for schema in schemas:
        assert schema.namespace_id == namespace.id
        assert schema.revision == 1
    assert [schema.status for schema in schemas] == [
        LifecycleStatus.ACTIVE,
        LifecycleStatus.MUTABLE,
        LifecycleStatus.ACTIVE,
        LifecycleStatus.MUTABLE,
    ]
    assert ResourceSchema.model_validate(copy).copied_from_id == source.id


def test_backend_get_is_namespace_scoped(db_session):
    amx = _namespace("backend/amx")
    fmx = _namespace("backend/fmx")
    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    with factory.begin() as session:
        session.add_all(
            [
                amx,
                fmx,
                ProcessTemplate(namespace=amx, name="data acquisition", version="1"),
                ProcessTemplate(namespace=fmx, name="data acquisition", version="1"),
            ]
        )
    backend = LocalBackend(factory)
    amx_template = backend.get_process_template(
        amx.id, "data acquisition", "1", expand=False
    )
    fmx_template = backend.get_process_template(
        fmx.id, "data acquisition", "1", expand=False
    )

    assert amx_template.namespace_id == amx.id
    assert fmx_template.namespace_id == fmx.id
    assert (
        backend.get_process_template(amx.id, "data acquisition", "1", expand=False).id
        == amx_template.id
    )
