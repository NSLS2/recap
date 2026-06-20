"""Performance + correctness tests for resource-subtree hydration on process
run queries.

When a process run is queried with ``include("resources")`` (or
``load="full"``), each assigned resource is hydrated together with its full
child hierarchy. That subtree must be fetched with a **bounded, depth-independent**
number of SQL statements: the assigned resources and all their descendants are
bulk-fetched in one query and the schema tree is built from that flat result.

The regression these tests guard against is hydrating each assigned resource by
walking ``resource.children.values()`` in Python, which lazy-loads every level
on demand -- an N+1 that scales with the assigned resource's tree depth.
"""

from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from recap.adapter.local import LocalBackend
from recap.db.campaign import Campaign
from recap.db.process import ProcessRun, ProcessTemplate, ResourceSlot
from recap.db.resource import Resource, ResourceTemplate, ResourceType
from recap.db.step import StepTemplate
from recap.dsl.query import QueryDSL
from recap.utils.general import Direction

from .conftest import count_statements


def make_query(db_session, campaign_id=None):
    session_local = sessionmaker(bind=db_session.get_bind())
    backend = LocalBackend(session_local)
    return QueryDSL(backend, campaign_id=campaign_id)


def _seed_run_with_resource_chain(db_session, *, name, depth):
    """Create a process run with one assigned resource that roots a linear
    child chain of ``depth`` nodes. Returns the ProcessRun."""
    with db_session.no_autoflush:
        campaign = Campaign(name=f"Campaign-{name}", proposal=f"PROP-{name}")
        template = ProcessTemplate(name=f"Template-{name}", version="1.0")
        step_template = StepTemplate(name=f"Step-{name}", process_template=template)
        run = ProcessRun(
            name=f"Run-{name}",
            description=f"Process run for {name}",
            template=template,
            campaign=campaign,
        )

    db_session.add_all([campaign, template, step_template])
    db_session.flush()
    db_session.add(run)
    db_session.flush()

    resource_type = ResourceType(name=f"resource-type-{uuid4().hex}")
    resource_template = ResourceTemplate(name=f"resource-template-{uuid4().hex}")
    resource_template.types.append(resource_type)
    slot = ResourceSlot(
        name=f"slot-{name}",
        process_template=template,
        resource_type=resource_type,
        direction=Direction.input,
    )

    root = Resource(name=f"{name}-0", template=resource_template)
    db_session.add_all([resource_type, resource_template, slot, root])
    parent = root
    for level in range(1, depth):
        child = Resource(
            name=f"{name}-{level}",
            template=resource_template,
            parent=parent,
        )
        db_session.add(child)
        parent = child

    db_session.flush()
    run.resources[slot] = root
    db_session.commit()
    return run


def _walk_depth(resource):
    """Return the number of nodes along the (single) child chain."""
    n = 1
    children = list(resource.children.values())
    while children:
        n += 1
        children = list(children[0].children.values())
    return n


def test_process_run_include_resources_is_depth_independent(db_session):
    """``include('resources')`` on a process run query must issue a bounded,
    depth-independent number of SQL statements regardless of the assigned
    resource's tree depth."""
    run_3 = _seed_run_with_resource_chain(db_session, name="three", depth=3)
    run_4 = _seed_run_with_resource_chain(db_session, name="four", depth=4)

    target = db_session.get_bind()

    with count_statements(target) as counter_3:
        loaded_3 = (
            make_query(db_session)
            .process_runs()
            .filter(id=run_3.id)
            .include_resources()
            .first()
        )
    n_three = counter_3["n"]

    with count_statements(target) as counter_4:
        loaded_4 = (
            make_query(db_session)
            .process_runs()
            .filter(id=run_4.id)
            .include_resources()
            .first()
        )
    n_four = counter_4["n"]

    root_3 = next(iter(loaded_3.assigned_resources.values())).resource
    root_4 = next(iter(loaded_4.assigned_resources.values())).resource
    assert _walk_depth(root_3) == 3
    assert _walk_depth(root_4) == 4

    assert n_four == n_three, (
        f"process-run resource hydration is depth-dependent (N+1): "
        f"3-level={n_three} statements, 4-level={n_four} statements"
    )


def test_process_run_include_resources_hydrates_full_chain(db_session):
    """The assigned resource's entire child chain must be hydrated with the
    correct structure (not just bounded statement count)."""
    run = _seed_run_with_resource_chain(db_session, name="chain", depth=4)

    loaded = (
        make_query(db_session)
        .process_runs()
        .filter(id=run.id)
        .include_resources()
        .first()
    )

    root = next(iter(loaded.assigned_resources.values())).resource

    # Walk the single chain and assert each node's name + single child key.
    node = root
    for level in range(4):
        assert node.name == f"chain-{level}"
        if level < 3:
            assert set(node.children) == {f"chain-{level + 1}"}
            node = node.children[f"chain-{level + 1}"]
        else:
            assert node.children == {}
