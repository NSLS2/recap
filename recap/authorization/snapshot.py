import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from recap.authentication.models import ProviderIdentity
from recap.authorization.scopes import Scope
from recap.exceptions import RecapServiceUnavailableError


class SnapshotUnavailable(RecapServiceUnavailableError):
    pass


class SnapshotMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    format_version: int
    source_revision: str


class GrantProvenance(BaseModel):
    model_config = ConfigDict(frozen=True)

    identity: ProviderIdentity
    namespace_path: str
    scope: Scope
    group: str
    role: str


class AuthorizationSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    metadata: SnapshotMetadata
    grants: frozenset[GrantProvenance]


def _seconds(value: float | datetime | timedelta) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, timedelta):
        return value.total_seconds()
    return float(value)


def load_authorization_snapshot(
    path: str | Path,
    *,
    max_age: float | timedelta,
    now: float | datetime | None = None,
) -> AuthorizationSnapshot:
    snapshot_path = Path(path)
    try:
        modified = snapshot_path.stat().st_mtime
    except OSError as error:
        raise SnapshotUnavailable("Authorization snapshot is unavailable") from error

    current_time = time.time() if now is None else _seconds(now)
    if current_time - modified > _seconds(max_age):
        raise SnapshotUnavailable("Authorization snapshot is stale")

    try:
        with sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            if integrity != [("ok",)]:
                raise SnapshotUnavailable(
                    "Authorization snapshot failed integrity check"
                )

            metadata_rows = connection.execute(
                "SELECT format_version, source_revision FROM snapshot_metadata"
            ).fetchall()
            if len(metadata_rows) != 1:
                raise SnapshotUnavailable("Authorization snapshot metadata is invalid")
            format_version, source_revision = metadata_rows[0]
            if format_version != 1:
                raise SnapshotUnavailable(
                    f"Unsupported authorization snapshot format version: {format_version}"
                )

            grant_rows = connection.execute(
                """
                SELECT i.provider, i.subject, n.path, s.name,
                       g.group_name, g.role_name
                FROM grants AS g
                JOIN identities AS i ON i.id = g.identity_id
                JOIN namespace_paths AS n ON n.id = g.namespace_path_id
                JOIN scopes AS s ON s.id = g.scope_id
                """
            ).fetchall()

        return AuthorizationSnapshot(
            metadata=SnapshotMetadata(
                format_version=format_version,
                source_revision=source_revision,
            ),
            grants=frozenset(
                GrantProvenance(
                    identity=ProviderIdentity(provider=provider, subject=subject),
                    namespace_path=namespace_path,
                    scope=scope,
                    group=group,
                    role=role,
                )
                for provider, subject, namespace_path, scope, group, role in grant_rows
            ),
        )
    except SnapshotUnavailable:
        raise
    except (OSError, sqlite3.Error, TypeError, ValidationError, ValueError) as error:
        raise SnapshotUnavailable("Authorization snapshot is invalid") from error


class SnapshotProvider:
    def __init__(
        self,
        path: str | Path,
        *,
        max_age: float | timedelta,
        clock: Callable[[], float | datetime] = time.time,
    ) -> None:
        self._path = Path(path)
        self._max_age = max_age
        self._clock = clock
        self._lock = threading.Lock()
        self._file_version: tuple[int, int, int] | None = None
        self._snapshot: AuthorizationSnapshot | None = None

    def acquire(self) -> AuthorizationSnapshot:
        with self._lock:
            try:
                stat = self._path.stat()
            except OSError as error:
                raise SnapshotUnavailable(
                    "Authorization snapshot is unavailable"
                ) from error
            version = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
            if version != self._file_version:
                snapshot = load_authorization_snapshot(
                    self._path,
                    max_age=self._max_age,
                    now=self._clock(),
                )
                self._snapshot = snapshot
                self._file_version = version
            if self._snapshot is None:
                raise SnapshotUnavailable("Authorization snapshot is unavailable")
            return self._snapshot
