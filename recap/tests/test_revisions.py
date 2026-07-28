from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from recap.commands.errors import CommandConflictError
from recap.db.base import Base, compare_and_swap_revision
from recap.db.namespace import Namespace


def test_create_starts_at_revision_one_and_patch_increments_once():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)

    with session_factory.begin() as session:
        namespace = Namespace(path=f"test/{uuid4()}", metadata_json={})
        session.add(namespace)
        session.flush()
        namespace_id = namespace.id
        assert namespace.revision == 1

        revision = compare_and_swap_revision(
            session,
            Namespace,
            namespace_id,
            expected_revision=1,
            values={"metadata_json": {"owner": "beamline"}},
        )

    assert revision == 2
    with session_factory() as session:
        namespace = session.get(Namespace, namespace_id)
        assert namespace.revision == 2
        assert namespace.metadata_json == {"owner": "beamline"}


def test_stale_expected_revision_fails_without_mutation():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)

    with session_factory.begin() as session:
        namespace = Namespace(path=f"test/{uuid4()}", metadata_json={})
        session.add(namespace)
        session.flush()
        namespace_id = namespace.id

        with pytest.raises(CommandConflictError, match="revision"):
            compare_and_swap_revision(
                session,
                Namespace,
                namespace_id,
                expected_revision=2,
                values={"metadata_json": {"owner": "wrong"}},
            )

    with session_factory() as session:
        namespace = session.get(Namespace, namespace_id)
        assert namespace.revision == 1
        assert namespace.metadata_json == {}


def test_rollback_leaves_revision_and_values_unchanged():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)

    with session_factory.begin() as session:
        namespace = Namespace(path=f"test/{uuid4()}", metadata_json={})
        session.add(namespace)
        session.flush()
        namespace_id = namespace.id

    with pytest.raises(RuntimeError, match="later failure"):
        with session_factory.begin() as session:
            compare_and_swap_revision(
                session,
                Namespace,
                namespace_id,
                expected_revision=1,
                values={"metadata_json": {"owner": "rolled-back"}},
            )
            raise RuntimeError("later failure")

    with session_factory() as session:
        namespace = session.get(Namespace, namespace_id)
        assert namespace.revision == 1
        assert namespace.metadata_json == {}
