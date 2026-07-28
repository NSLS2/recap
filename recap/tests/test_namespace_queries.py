from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from recap.adapter.local import LocalBackend
from recap.adapter.transport import QueryRequest
from recap.db.campaign import Campaign
from recap.db.namespace import Namespace
from recap.db.process import ProcessRun, ProcessTemplate
from recap.db.resource import Resource, ResourceTemplate
from recap.dsl.query import QueryDSL, QuerySpec
from recap.lifecycle import LifecycleStatus
from recap.schemas.namespace import NamespaceContext
from recap.schemas.resource import ResourceTemplateSchema


def _namespace(path, *, parent=None, metadata=None):
    return Namespace(
        id=uuid4(),
        path=path,
        parent=parent,
        metadata_json=metadata or {},
        status=LifecycleStatus.ACTIVE,
    )


def _query(db_session, namespace):
    backend = LocalBackend(sessionmaker(bind=db_session.get_bind()))
    context = NamespaceContext(id=namespace.id, path=namespace.path)
    return QueryDSL(backend, context=context)


def test_namespace_context_is_transport_context_not_query_spec():
    request = QueryRequest.from_query(
        ResourceTemplateSchema,
        QuerySpec(),
        namespace_path="beamline/amx/proposal/312345",
    )

    assert request.namespace_path == "beamline/amx/proposal/312345"
    assert "namespace_path" not in request.spec
    assert "campaign_id" not in request.spec


def test_process_runs_are_visible_only_in_exact_namespace(db_session):
    parent = _namespace("beamline/amx")
    own = _namespace("beamline/amx/proposal/1", parent=parent)
    sibling = _namespace("beamline/amx/proposal/2", parent=parent)
    campaigns = [
        Campaign(id=namespace.id, name=namespace.path, proposal=namespace.path)
        for namespace in (parent, own, sibling)
    ]
    templates = [
        ProcessTemplate(
            namespace=namespace,
            name=f"template-{index}",
            version="1",
            status=LifecycleStatus.ACTIVE,
        )
        for index, namespace in enumerate((parent, own, sibling))
    ]
    runs = [
        ProcessRun(
            namespace=namespace,
            name=f"run-{index}",
            description="run",
            template=template,
            campaign=campaign,
            status=LifecycleStatus.ACTIVE,
        )
        for index, (namespace, template, campaign) in enumerate(
            zip((parent, own, sibling), templates, campaigns, strict=True)
        )
    ]
    db_session.add_all([parent, own, sibling, *campaigns, *templates, *runs])
    db_session.commit()

    result = _query(db_session, own).process_runs(shape="ref").all()

    assert [item.name for item in result] == ["run-1"]


def test_templates_inherit_active_ancestors_and_archive_is_opt_in(db_session):
    parent = _namespace("beamline/fmx")
    own = _namespace("beamline/fmx/proposal/1", parent=parent)
    sibling = _namespace("beamline/fmx/proposal/2", parent=parent)
    templates = [
        ProcessTemplate(
            namespace=parent,
            name="ancestor",
            version="1",
            labels=["MX Data Acquisition"],
            status=LifecycleStatus.ACTIVE,
        ),
        ProcessTemplate(
            namespace=own,
            name="own-active",
            version="1",
            labels=["MX Data Acquisition"],
            status=LifecycleStatus.ACTIVE,
        ),
        ProcessTemplate(
            namespace=own,
            name="own-mutable",
            version="1",
            status=LifecycleStatus.MUTABLE,
        ),
        ProcessTemplate(
            namespace=own,
            name="own-archived",
            version="1",
            status=LifecycleStatus.ARCHIVED,
        ),
        ProcessTemplate(
            namespace=sibling,
            name="sibling",
            version="1",
            status=LifecycleStatus.ACTIVE,
        ),
    ]
    db_session.add_all([parent, own, sibling, *templates])
    db_session.commit()

    query = _query(db_session, own).process_templates(shape="ref")
    assert {item.name for item in query.all()} == {"ancestor", "own-active"}
    assert {item.name for item in query.include_archived().all()} == {
        "ancestor",
        "own-active",
        "own-archived",
    }
    assert {item.name for item in query.filter_label("mx_data_acquisition").all()} == {
        "ancestor",
        "own-active",
    }


def test_visibility_precedes_filter_pagination_and_count(db_session):
    root = _namespace("facility")
    own = _namespace("facility/proposal", parent=root)
    sibling = _namespace("facility/other", parent=root)
    rows = [
        ProcessTemplate(
            namespace=namespace,
            name=name,
            version="1",
            status=LifecycleStatus.ACTIVE,
        )
        for namespace, name in (
            (root, "a"),
            (own, "b"),
            (own, "c"),
            (sibling, "d"),
        )
    ]
    db_session.add_all([root, own, sibling, *rows])
    db_session.commit()

    query = (
        _query(db_session, own)
        .process_templates(shape="ref")
        .order_by(ProcessTemplate.name)
    )
    assert [item.name for item in query.offset(1).limit(1).all()] == ["b"]
    assert query.count() == 3


def test_resources_and_resource_templates_inherit_active_ancestors(db_session):
    parent = _namespace("resource/amx")
    own = _namespace("resource/amx/proposal", parent=parent)
    sibling = _namespace("resource/fmx")
    ancestor_template = ResourceTemplate(
        namespace=parent,
        name="ancestor-template",
        version="1",
        labels=["MX Data Acquisition"],
        status=LifecycleStatus.ACTIVE,
    )
    own_template = ResourceTemplate(
        namespace=own,
        name="own-template",
        version="1",
        labels=["MX Data Acquisition"],
        status=LifecycleStatus.ACTIVE,
    )
    sibling_template = ResourceTemplate(
        namespace=sibling,
        name="sibling-template",
        version="1",
        status=LifecycleStatus.ACTIVE,
    )
    resources = [
        Resource(
            namespace=namespace,
            name=name,
            template=template,
            status=LifecycleStatus.ACTIVE,
        )
        for namespace, name, template in (
            (parent, "ancestor-resource", ancestor_template),
            (own, "own-resource", own_template),
            (sibling, "sibling-resource", sibling_template),
        )
    ]
    db_session.add_all(
        [
            parent,
            own,
            sibling,
            ancestor_template,
            own_template,
            sibling_template,
            *resources,
        ]
    )
    db_session.commit()

    query = _query(db_session, own)
    assert {item.name for item in query.resources(shape="ref").all()} == {
        "ancestor-resource",
        "own-resource",
    }
    assert {
        item.name
        for item in query.resource_templates(shape="ref")
        .filter_label("mx_data_acquisition")
        .all()
    } == {"ancestor-template", "own-template"}


def test_namespace_metadata_filters_distinguish_local_and_effective(db_session):
    parent = _namespace("metadata/amx", metadata={"facility": "nsls2"})
    own = _namespace(
        "metadata/amx/proposal",
        parent=parent,
        metadata={"proposal": "123", "facility": "override"},
    )
    db_session.add_all([parent, own])
    db_session.commit()

    query = _query(db_session, own).namespaces()
    assert [
        item.path for item in query.filter_local_metadata(facility="nsls2").all()
    ] == [parent.path]
    assert [
        item.path for item in query.filter_effective_metadata(proposal="123").all()
    ] == [own.path]
    assert [
        item.path for item in query.filter_effective_metadata(facility="override").all()
    ] == [own.path]
