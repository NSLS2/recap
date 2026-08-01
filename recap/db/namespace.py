from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, ForeignKey, select
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from recap.utils.namespace import canonicalize_namespace_path, namespace_ancestors

from .base import Base, RevisionedLifecycleMixin, TimestampMixin

SiteMetadataValidator = Callable[
    [Mapping[str, Any], Mapping[str, Any]],
    None,
]


class Namespace(RevisionedLifecycleMixin, TimestampMixin, Base):
    __tablename__ = "namespace"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    path: Mapped[str] = mapped_column(unique=True, nullable=False)
    parent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("namespace.id"), nullable=True
    )
    parent: Mapped["Namespace | None"] = relationship(
        remote_side=[id], back_populates="children"
    )
    children: Mapped[list["Namespace"]] = relationship(back_populates="parent")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), nullable=False, default=dict
    )


class NamespaceRepository:
    def __init__(
        self,
        session: Session,
        site_validator: SiteMetadataValidator | None = None,
    ) -> None:
        self._session = session
        self._site_validator = site_validator

    def create(self, path: str, metadata: Mapping[str, Any] | None = None) -> Namespace:
        path = canonicalize_namespace_path(path)
        local_metadata = dict(metadata or {})
        ancestor_paths = namespace_ancestors(path)[:-1]
        parent = None
        if ancestor_paths:
            ancestors = {
                item.path: item
                for item in self._session.scalars(
                    select(Namespace).where(Namespace.path.in_(ancestor_paths))
                )
            }
            parent = next(
                (
                    ancestors[item]
                    for item in reversed(ancestor_paths)
                    if item in ancestors
                ),
                None,
            )

        inherited = self.effective_metadata(parent.id) if parent is not None else {}
        if self._site_validator is not None:
            self._site_validator(inherited, local_metadata)

        namespace = Namespace(
            path=path,
            parent=parent,
            metadata_json=local_metadata,
        )
        self._session.add(namespace)
        return namespace

    def effective_metadata(self, namespace_id: UUID) -> dict[str, Any]:
        namespace = self._session.get(Namespace, namespace_id)
        if namespace is None:
            raise LookupError(f"Namespace does not exist: {namespace_id}")

        lineage = []
        current: Namespace | None = namespace
        while current is not None:
            lineage.append(current)
            current = current.parent

        effective: dict[str, Any] = {}
        for item in reversed(lineage):
            effective.update(item.metadata_json)
        return effective
