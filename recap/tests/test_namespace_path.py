import pytest

from recap.utils.namespace import (
    canonicalize_namespace_path,
    is_namespace_ancestor,
    namespace_ancestors,
    parent_namespace_path,
)


def test_namespace_path_and_ancestors():
    path = canonicalize_namespace_path("beamline/amx/proposal/312345")
    assert path == "beamline/amx/proposal/312345"
    assert parent_namespace_path(path) == "beamline/amx/proposal"
    assert namespace_ancestors(path) == (
        "",
        "beamline",
        "beamline/amx",
        "beamline/amx/proposal",
        path,
    )
    assert is_namespace_ancestor("beamline/amx", path)
    assert not is_namespace_ancestor("beamline/am", path)


@pytest.mark.parametrize("path", ["/amx", "amx/", "a//b", "a/../b", "a/%2fb"])
def test_rejects_noncanonical_namespace_paths(path):
    with pytest.raises(ValueError, match="Namespace path"):
        canonicalize_namespace_path(path)
