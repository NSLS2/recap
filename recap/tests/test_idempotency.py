from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from recap.commands.errors import CommandConflictError
from recap.commands.idempotency import command_fingerprint
from recap.db.base import Base
from recap.db.idempotency import IdempotencyRepository


class Body(BaseModel):
    name: str
    optional: str | None = None


def fingerprint(**overrides) -> str:
    values = {
        "method": "PATCH",
        "route_template": "/namespaces/{namespace}/resources/{source_id}",
        "namespace_path": "beamline/endstation",
        "source_id": uuid4(),
        "body": Body(name="detector"),
    }
    values.update(overrides)
    return command_fingerprint(**values)


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("method", "POST"),
        ("route_template", "/namespaces/{namespace}/resources"),
        ("namespace_path", "beamline/other"),
        ("source_id", uuid4()),
        ("body", Body(name="motor")),
    ],
)
def test_fingerprint_covers_exact_command_identity(field, changed):
    source_id = uuid4()
    original = fingerprint(source_id=source_id)
    changed_values = {"source_id": source_id, field: changed}

    assert fingerprint(**changed_values) != original
    assert fingerprint(source_id=source_id) == original


def test_same_actor_key_and_fingerprint_replays_exact_result_after_authorization():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    actor_id = "actor-1"
    key = "command-1"
    command = fingerprint()
    target_id = str(uuid4())
    response = {"id": target_id, "revision": 2, "nested": {"state": "ready"}}

    with session_factory.begin() as session:
        repository = IdempotencyRepository(session)
        decision = repository.claim(actor_id, key, command, lambda _: None)
        assert not decision.replayed
        repository.complete(decision, target_id=target_id, response=response)

    checked = []
    with session_factory.begin() as session:
        decision = IdempotencyRepository(session).claim(
            actor_id, key, command, checked.append
        )

    assert decision.replayed
    assert decision.target_id == target_id
    assert decision.response == response
    assert checked == [target_id]


def test_revoked_authority_denies_replay_before_result_is_returned():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    command = fingerprint()

    with session_factory.begin() as session:
        repository = IdempotencyRepository(session)
        decision = repository.claim("actor-1", "command-1", command, lambda _: None)
        repository.complete(decision, target_id="resource-1", response={"secret": False})

    def deny(_target_id):
        raise PermissionError("authority revoked")

    with session_factory.begin() as session:
        with pytest.raises(PermissionError, match="authority revoked"):
            IdempotencyRepository(session).claim(
                "actor-1", "command-1", command, deny
            )


def test_actor_and_key_are_unique_and_changed_fingerprint_conflicts():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    original = fingerprint()

    with session_factory.begin() as session:
        repository = IdempotencyRepository(session)
        first = repository.claim("actor-1", "command-1", original, lambda _: None)
        repository.complete(first, target_id="resource-1", response={"revision": 1})

    with session_factory.begin() as session:
        with pytest.raises(CommandConflictError, match="different command"):
            IdempotencyRepository(session).claim(
                "actor-1", "command-1", fingerprint(), lambda _: None
            )

    with session_factory.begin() as session:
        other_actor = IdempotencyRepository(session).claim(
            "actor-2", "command-1", original, lambda _: None
        )
    assert not other_actor.replayed


def test_concurrent_claim_has_one_winner_and_one_authorized_replay(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'idempotency.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    winner_claimed = Event()
    release_winner = Event()
    command = fingerprint()

    def run(first: bool):
        with session_factory.begin() as session:
            repository = IdempotencyRepository(session)
            if not first:
                winner_claimed.wait(timeout=5)
            decision = repository.claim(
                "actor-1", "shared-key", command, lambda _: None
            )
            if first:
                winner_claimed.set()
                release_winner.wait(timeout=5)
                repository.complete(
                    decision,
                    target_id="resource-1",
                    response={"id": "resource-1", "revision": 1},
                )
            return decision

    with ThreadPoolExecutor(max_workers=2) as executor:
        winner = executor.submit(run, True)
        loser = executor.submit(run, False)
        assert winner_claimed.wait(timeout=5)
        release_winner.set()
        decisions = [winner.result(timeout=5), loser.result(timeout=5)]

    assert sorted(decision.replayed for decision in decisions) == [False, True]
    replay = next(decision for decision in decisions if decision.replayed)
    assert replay.target_id == "resource-1"
    assert replay.response == {"id": "resource-1", "revision": 1}
