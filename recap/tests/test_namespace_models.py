from collections.abc import Mapping
from typing import Any

import pytest

from recap.db.namespace import Namespace, NamespaceRepository
from recap.lifecycle import LifecycleStatus
from recap.schemas.namespace import NamespaceContext, NamespaceRef, NamespaceSchema


@pytest.fixture
def namespace_repository(db_session):
    return NamespaceRepository(db_session)


def test_namespace_round_trip(db_session):
    root = Namespace(path="beamline", metadata_json={"facility": "nsls2"})
    child = Namespace(
        path="beamline/amx", parent=root, metadata_json={"beamline": "amx"}
    )
    db_session.add_all([root, child])
    db_session.flush()

    schema = NamespaceSchema.model_validate(child)
    assert schema.path == "beamline/amx"
    assert schema.parent_id == root.id
    assert schema.status is LifecycleStatus.MUTABLE
    assert schema.revision == 1
    assert schema.metadata == {"beamline": "amx"}
    assert schema.model_dump()["metadata"] == {"beamline": "amx"}
    assert NamespaceRef.model_validate(child).path == child.path
    assert NamespaceContext(id=child.id, path=child.path).path == child.path


def test_effective_metadata_merges_ancestors(namespace_repository, db_session):
    namespace_repository.create("beamline")
    root = namespace_repository.create("beamline/amx", {"facility": "nsls2"})
    namespace_repository.create("beamline/amx/proposal")
    child = namespace_repository.create(
        "beamline/amx/proposal/312345", {"nsls2.proposal": "312345"}
    )
    db_session.flush()
    assert child.parent.path == "beamline/amx/proposal"
    assert namespace_repository.effective_metadata(child.id) == {
        "facility": "nsls2",
        "nsls2.proposal": "312345",
    }


def test_site_validator_rejects_conflicting_protected_metadata(db_session):
    def reject_conflicts(
        inherited: Mapping[str, Any], local: Mapping[str, Any]
    ) -> None:
        if (
            "facility" in inherited
            and "facility" in local
            and local["facility"] != inherited["facility"]
        ):
            raise ValueError("facility metadata is protected")

    repository = NamespaceRepository(db_session, site_validator=reject_conflicts)
    repository.create("facility", {"facility": "nsls2"})
    db_session.flush()

    with pytest.raises(ValueError, match="facility metadata is protected"):
        repository.create("facility/beamline", {"facility": "other"})
