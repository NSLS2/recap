import os
import tempfile

from recap.adapter import (
    AuthorizedReadBackend,
    NamespaceCatalog,
    NamespaceContextResolver,
    NamespaceWriter,
    ReadBackend,
    WriteBackend,
)
from recap.adapter.local import LocalBackend


def test_read_backend_is_protocol():
    from typing import Protocol

    assert issubclass(ReadBackend, Protocol) or hasattr(
        ReadBackend, "__protocol_attrs__"
    )


def test_write_backend_is_protocol():
    from typing import Protocol

    assert issubclass(WriteBackend, Protocol) or hasattr(
        WriteBackend, "__protocol_attrs__"
    )


def test_local_backend_satisfies_independent_capabilities():
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "test.db")
        lb = LocalBackend(db_path)
        assert isinstance(lb, ReadBackend)
        assert isinstance(lb, WriteBackend)
        assert isinstance(lb, NamespaceCatalog)
        assert isinstance(lb, NamespaceContextResolver)
        assert isinstance(lb, NamespaceWriter)


def test_authorized_read_backend_is_separate_from_public_read_backend():
    assert "query_authorized" not in ReadBackend.__dict__
    assert "count_authorized" not in ReadBackend.__dict__
    assert "query_authorized" in AuthorizedReadBackend.__dict__
    assert "count_authorized" in AuthorizedReadBackend.__dict__
