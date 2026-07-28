import re

_NAMESPACE_SEGMENT = re.compile(r"[a-z0-9._~-]+")


def canonicalize_namespace_path(path: str) -> str:
    if path == "":
        return path

    segments = path.split("/")
    if any(
        segment in {"", ".", ".."} or _NAMESPACE_SEGMENT.fullmatch(segment) is None
        for segment in segments
    ):
        raise ValueError(f"Namespace path is not canonical: {path!r}")
    return path


def parent_namespace_path(path: str) -> str:
    path = canonicalize_namespace_path(path)
    if path == "":
        return path
    return path.rpartition("/")[0]


def namespace_ancestors(path: str) -> tuple[str, ...]:
    path = canonicalize_namespace_path(path)
    if path == "":
        return (path,)

    segments = path.split("/")
    return ("", *("/".join(segments[:index]) for index in range(1, len(segments) + 1)))


def is_namespace_ancestor(ancestor: str, path: str) -> bool:
    ancestor = canonicalize_namespace_path(ancestor)
    path = canonicalize_namespace_path(path)
    return ancestor == "" or path == ancestor or path.startswith(f"{ancestor}/")
