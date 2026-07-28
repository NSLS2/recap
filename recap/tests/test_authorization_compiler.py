import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from recap.authorization import compiler
from recap.authorization.compiler import compile_authorization_snapshot
from recap.authorization.source import load_authorization_source

FIXTURE = Path(__file__).parent / "fixtures" / "authorization.yml"


def _write_source(tmp_path, text):
    source = tmp_path / "authorization.yml"
    source.write_text(text)
    return source


def _valid_source(body=""):
    return f"""\
source_revision: revision-1
roles:
  reader:
    scopes: [namespace:read]
groups:
  scientists:
    identities:
      - provider: pam
        subject: alice
namespaces:
  beamline/amx:
    groups:
      - name: scientists
        role: reader
{body}"""


def test_compiles_exact_flattened_grants_with_provenance(tmp_path):
    snapshot = tmp_path / "authorization.db"

    compile_authorization_snapshot(FIXTURE, snapshot)

    with sqlite3.connect(snapshot) as connection:
        metadata = connection.execute(
            "SELECT format_version, source_revision FROM snapshot_metadata"
        ).fetchone()
        rows = connection.execute(
            """
            SELECT i.provider || ':' || i.subject, n.path, s.name,
                   g.group_name, g.role_name
            FROM grants AS g
            JOIN identities AS i ON i.id = g.identity_id
            JOIN namespace_paths AS n ON n.id = g.namespace_path_id
            JOIN scopes AS s ON s.id = g.scope_id
            ORDER BY 1, 2, 3, 4, 5
            """
        ).fetchall()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()

    assert metadata == (1, "revision-42")
    assert rows == [
        (
            "oidc:bob@example.com",
            "beamline/amx",
            "namespace:read",
            "scientists",
            "reader",
        ),
        (
            "oidc:bob@example.com",
            "beamline/amx",
            "resource:read",
            "scientists",
            "reader",
        ),
        (
            "pam:alice",
            "beamline/amx",
            "namespace:read",
            "scientists",
            "reader",
        ),
        (
            "pam:alice",
            "beamline/amx",
            "resource:read",
            "scientists",
            "reader",
        ),
        (
            "pam:carol",
            "beamline/amx/proposal/312345",
            "namespace:read",
            "operators",
            "operator",
        ),
        (
            "pam:carol",
            "beamline/amx/proposal/312345",
            "process-run:write",
            "operators",
            "operator",
        ),
    ]
    assert integrity == ("ok",)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            _valid_source().replace("namespace:read", "unknown:scope"),
            "unknown:scope",
        ),
        (_valid_source().replace("role: reader", "role: missing"), "missing"),
        (
            _valid_source().replace("name: scientists", "name: missing"),
            "missing",
        ),
        (_valid_source().replace("beamline/amx:", "/beamline/amx:"), "path"),
        (_valid_source().replace("scopes: [namespace:read]", "scopes: []"), "scopes"),
        (
            _valid_source().replace(
                "namespaces:",
                """\
  duplicate:
    identities:
      - provider: pam
        subject: alice
namespaces:
"""
            ),
            "Duplicate identity",
        ),
        (
            _valid_source().replace(
                "      - name: scientists\n        role: reader",
                "      - name: scientists\n        role: reader\n"
                "      - name: scientists\n        role: reader",
            ),
            "Duplicate binding",
        ),
    ],
)
def test_rejects_invalid_authorization_sources(tmp_path, source, message):
    with pytest.raises((ValidationError, ValueError), match=message):
        load_authorization_source(_write_source(tmp_path, source))


def test_rejects_duplicate_yaml_mapping_keys(tmp_path):
    source = _valid_source().replace(
        "source_revision: revision-1",
        "source_revision: revision-1\nsource_revision: revision-2",
    )

    with pytest.raises(ValueError, match="Duplicate YAML key.*source_revision"):
        load_authorization_source(_write_source(tmp_path, source))


def test_rejects_unknown_fields(tmp_path):
    with pytest.raises(ValidationError, match="extra_forbidden"):
        load_authorization_source(_write_source(tmp_path, _valid_source("extra: true\n")))


def test_failed_compile_preserves_existing_snapshot_and_removes_temp(tmp_path):
    snapshot = tmp_path / "authorization.db"
    snapshot.write_bytes(b"existing snapshot")
    malformed = _write_source(
        tmp_path, _valid_source().replace("role: reader", "role: missing")
    )

    with pytest.raises(ValueError, match="missing"):
        compile_authorization_snapshot(malformed, snapshot)

    assert snapshot.read_bytes() == b"existing snapshot"
    assert list(tmp_path.glob(".authorization.db.*.tmp")) == []


def test_replace_failure_preserves_existing_snapshot_and_removes_temp(
    tmp_path, monkeypatch
):
    snapshot = tmp_path / "authorization.db"
    snapshot.write_bytes(b"existing snapshot")

    def fail_replace(source, destination):
        assert Path(source).parent == snapshot.parent
        assert Path(destination) == snapshot
        raise OSError("replace failed")

    monkeypatch.setattr(compiler.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        compile_authorization_snapshot(FIXTURE, snapshot)

    assert snapshot.read_bytes() == b"existing snapshot"
    assert list(tmp_path.glob(".authorization.db.*.tmp")) == []
