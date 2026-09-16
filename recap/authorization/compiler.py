import argparse
import contextlib
import os
import sqlite3
import tempfile
from pathlib import Path

from recap.authorization.source import (
    AuthorizationSourceConfig,
    load_authorization_source,
)

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE snapshot_metadata (
    format_version INTEGER NOT NULL CHECK (format_version = 1),
    source_revision TEXT NOT NULL
);

CREATE TABLE identities (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    subject TEXT NOT NULL,
    UNIQUE (provider, subject)
);

CREATE TABLE namespace_paths (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE
);

CREATE TABLE scopes (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE grants (
    identity_id INTEGER NOT NULL REFERENCES identities(id),
    namespace_path_id INTEGER NOT NULL REFERENCES namespace_paths(id),
    scope_id INTEGER NOT NULL REFERENCES scopes(id),
    group_name TEXT NOT NULL,
    role_name TEXT NOT NULL,
    UNIQUE (identity_id, namespace_path_id, scope_id, group_name, role_name)
);
"""


def _populate_snapshot(
    connection: sqlite3.Connection, source: AuthorizationSourceConfig
) -> None:
    connection.executescript(_SCHEMA)
    connection.execute(
        "INSERT INTO snapshot_metadata (format_version, source_revision) VALUES (1, ?)",
        (source.source_revision,),
    )

    identities = sorted(
        {
            (identity.provider, identity.subject)
            for group in source.groups.values()
            for identity in group.identities
        }
    )
    paths = sorted(source.namespaces)
    scopes = sorted(
        {
            scope.value
            for namespace in source.namespaces.values()
            for binding in namespace.groups
            for scope in source.roles[binding.role].scopes
        }
    )
    connection.executemany(
        "INSERT INTO identities (provider, subject) VALUES (?, ?)", identities
    )
    connection.executemany(
        "INSERT INTO namespace_paths (path) VALUES (?)", ((path,) for path in paths)
    )
    connection.executemany(
        "INSERT INTO scopes (name) VALUES (?)", ((scope,) for scope in scopes)
    )

    identity_ids = {
        (provider, subject): identity_id
        for identity_id, provider, subject in connection.execute(
            "SELECT id, provider, subject FROM identities"
        )
    }
    path_ids = dict(connection.execute("SELECT path, id FROM namespace_paths"))
    scope_ids = dict(connection.execute("SELECT name, id FROM scopes"))

    grants = []
    for path, namespace in source.namespaces.items():
        for binding in namespace.groups:
            role = source.roles[binding.role]
            for identity in source.groups[binding.name].identities:
                for scope in role.scopes:
                    grants.append(
                        (
                            identity_ids[(identity.provider, identity.subject)],
                            path_ids[path],
                            scope_ids[scope.value],
                            binding.name,
                            binding.role,
                        )
                    )
    connection.executemany(
        """
        INSERT INTO grants (
            identity_id, namespace_path_id, scope_id, group_name, role_name
        ) VALUES (?, ?, ?, ?, ?)
        """,
        grants,
    )


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def compile_authorization_snapshot(
    source_path: str | Path, snapshot_path: str | Path
) -> None:
    source = load_authorization_source(source_path)
    destination = Path(snapshot_path)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)

    try:
        with sqlite3.connect(temporary) as connection:
            _populate_snapshot(connection, source)
            connection.commit()
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            if integrity != [("ok",)]:
                raise RuntimeError(f"SQLite integrity check failed: {integrity!r}")
        _fsync_file(temporary)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        for artifact in (temporary, Path(f"{temporary}-journal")):
            with contextlib.suppress(FileNotFoundError):
                artifact.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compile a YAML authorization source into a SQLite snapshot."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("snapshot", type=Path)
    arguments = parser.parse_args()
    compile_authorization_snapshot(arguments.source, arguments.snapshot)


if __name__ == "__main__":
    main()
