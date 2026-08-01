import os
import tempfile

from recap.adapter import ReadBackend, WriteBackend
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


def test_local_backend_satisfies_backend():
    # LocalBackend must still satisfy the combined Backend protocol
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "test.db")
        lb = LocalBackend(db_path)
        # runtime_checkable would be ideal but Protocol doesn't require it;
        # just verify the key methods exist on both protocols
        assert hasattr(lb, "query")
        assert hasattr(lb, "create_namespace")
        assert hasattr(lb, "count")
        assert hasattr(lb, "create_resource")
