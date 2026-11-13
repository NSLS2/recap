import os
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from recap.authentication.models import ProviderIdentity
from recap.authorization.compiler import compile_authorization_snapshot
from recap.authorization.scopes import Scope
from recap.authorization.snapshot import (
    SnapshotProvider,
    SnapshotUnavailable,
    load_authorization_snapshot,
)

FIXTURE = Path(__file__).parent / "fixtures" / "authorization.yml"


def _compile(snapshot: Path, source_revision: str = "revision-42") -> None:
    source = snapshot.with_suffix(".yml")
    source.write_text(FIXTURE.read_text().replace("revision-42", source_revision))
    compile_authorization_snapshot(source, snapshot)


def test_loads_snapshot_read_only_with_immutable_grants(tmp_path, monkeypatch):
    path = tmp_path / "authorization.db"
    _compile(path)
    real_connect = sqlite3.connect
    connections = []

    def record_connect(database, *args, **kwargs):
        connections.append((database, kwargs))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", record_connect)

    snapshot = load_authorization_snapshot(path, max_age=60, now=path.stat().st_mtime)

    assert connections == [(f"file:{path}?mode=ro", {"uri": True})]
    assert snapshot.metadata.format_version == 1
    assert snapshot.metadata.source_revision == "revision-42"
    assert len(snapshot.grants) == 6
    assert any(
        grant.identity == ProviderIdentity(provider="pam", subject="alice")
        and grant.namespace_path == "beamline/amx"
        and grant.scope is Scope.RESOURCE_READ
        and grant.group == "scientists"
        and grant.role == "reader"
        for grant in snapshot.grants
    )
    with pytest.raises(ValidationError, match="frozen"):
        snapshot.metadata.source_revision = "changed"
    with pytest.raises(AttributeError):
        snapshot.grants.add(next(iter(snapshot.grants)))


def test_rejects_missing_snapshot(tmp_path):
    with pytest.raises(SnapshotUnavailable, match="unavailable"):
        load_authorization_snapshot(tmp_path / "missing.db", max_age=60, now=0)


def test_rejects_unsupported_schema_version(tmp_path):
    path = tmp_path / "authorization.db"
    _compile(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute("UPDATE snapshot_metadata SET format_version = 2")

    with pytest.raises(SnapshotUnavailable, match="format version"):
        load_authorization_snapshot(path, max_age=60, now=path.stat().st_mtime)


def test_rejects_corrupt_snapshot(tmp_path):
    path = tmp_path / "authorization.db"
    path.write_bytes(b"not sqlite")

    with pytest.raises(SnapshotUnavailable, match="invalid"):
        load_authorization_snapshot(path, max_age=60, now=path.stat().st_mtime)


def test_rejects_failed_integrity_check(tmp_path, monkeypatch):
    path = tmp_path / "authorization.db"
    _compile(path)
    real_connect = sqlite3.connect

    class Cursor:
        def fetchall(self):
            return [("corrupt",)]

    class Connection:
        def __init__(self, connection):
            self.connection = connection

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.connection.close()

        def execute(self, sql, parameters=()):
            if sql == "PRAGMA integrity_check":
                return Cursor()
            return self.connection.execute(sql, parameters)

    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda database, *args, **kwargs: Connection(
            real_connect(database, *args, **kwargs)
        ),
    )

    with pytest.raises(SnapshotUnavailable, match="integrity"):
        load_authorization_snapshot(path, max_age=60, now=path.stat().st_mtime)


def test_rejects_stale_snapshot(tmp_path):
    path = tmp_path / "authorization.db"
    _compile(path)
    modified = path.stat().st_mtime

    with pytest.raises(SnapshotUnavailable, match="stale"):
        load_authorization_snapshot(path, max_age=60, now=modified + 61)


def test_provider_atomically_reloads_generation_and_retains_acquired_snapshot(
    tmp_path,
):
    path = tmp_path / "authorization.db"
    _compile(path, "revision-1")
    now = path.stat().st_mtime
    provider = SnapshotProvider(path, max_age=60, clock=lambda: now)

    request_snapshot = provider.acquire()
    assert provider.acquire() is request_snapshot

    replacement = tmp_path / "replacement.db"
    _compile(replacement, "revision-2")
    os.replace(replacement, path)
    reloaded = provider.acquire()

    assert reloaded is not request_snapshot
    assert reloaded.metadata.source_revision == "revision-2"
    assert request_snapshot.metadata.source_revision == "revision-1"


def test_provider_fails_closed_when_replacement_is_corrupt(tmp_path):
    path = tmp_path / "authorization.db"
    _compile(path)
    now = path.stat().st_mtime
    provider = SnapshotProvider(path, max_age=60, clock=lambda: now)
    provider.acquire()
    replacement = tmp_path / "replacement.db"
    replacement.write_bytes(b"not sqlite")
    os.replace(replacement, path)

    with pytest.raises(SnapshotUnavailable, match="invalid"):
        provider.acquire()
    with pytest.raises(SnapshotUnavailable, match="invalid"):
        provider.acquire()
