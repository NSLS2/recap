import sqlalchemy
from .base import Base
from sqlalchemy import (
    Column,
    ForeignKey,
    Table,
)
from sqlalchemy.orm import declared_attr, relationship, mapped_column, Mapped, Session, validates
from sqlalchemy.ext.hybrid import hybrid_property
from typing import List, Optional, Set, Any
from uuid import uuid4, UUID
from .attribute import Attribute, AttributeValueMixin, container_type_attribute_association


class Property(Base, AttributeValueMixin):
    __tablename__ = "property"
    uid: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    container_id: Mapped[UUID] = mapped_column(ForeignKey("container.uid"), nullable=False)
    container: Mapped["Container"] = relationship(back_populates="properties")

    def __init__(self, *args, **kwargs):
        value = kwargs.pop("value", None)
        attribute = kwargs.get("attribute")
        super().__init__(*args, **kwargs)
        if value is None:
            value = attribute.default_value
        self.set_value(value)


class ContainerType(Base):
    __tablename__ = "container_type"
    uid: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(unique=True, nullable=True)
    short_name: Mapped[Optional[str]] = mapped_column(nullable=True)

    parent_id: Mapped[Optional[UUID]] = mapped_column(ForeignKey("container_type.uid"), nullable=True)
    parent: Mapped["ContainerType"] = relationship(back_populates="children", remote_side=[uid])

    children: Mapped[List["ContainerType"]] = relationship(back_populates="parent")

    attributes: Mapped[List["Attribute"]] = relationship(
        back_populates="container_types", secondary=container_type_attribute_association
    )


class Container(Base):
    __tablename__ = "container"
    uid: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(unique=True, nullable=True)
    short_name: Mapped[Optional[str]] = mapped_column(nullable=True)

    container_type_id: Mapped[UUID] = mapped_column(ForeignKey("container_type.uid"), nullable=True)
    container_type: Mapped["ContainerType"] = relationship()

    parent_id: Mapped[Optional[UUID]] = mapped_column(ForeignKey("container.uid"), nullable=True)
    parent: Mapped["Container"] = relationship(back_populates="children", remote_side=[uid])

    children: Mapped[List["Container"]] = relationship(back_populates="parent")

    properties: Mapped[List["Property"]] = relationship(back_populates="container")

    def __init__(
        self,
        *args,
        _init_children: bool = True,
        _visited_children: Optional[Set[UUID]] = None,
        _max_depth: int = 10,
        **kwargs
    ):
        container_type = kwargs.get("container_type", None)
        super().__init__(*args, **kwargs)

        if container_type and _init_children:
            self._initialize_from_container_type(container_type, _visited_children, _max_depth)

    def _initialize_from_container_type(
        self,
        container_type: Optional[ContainerType] = None,
        visited: Optional[Set[UUID]] = None,
        max_depth: int = 10,
    ):
        """
        Automatically initialize container from container_type
        - Use visited to avoid using the same container_type to prevent cycles
        - max_depth should prevent too many recursions
        - Only add properties if not present
        """
        if not container_type:
            return

        if max_depth <= 0:
            return

        if visited is None:
            visited = set()

        if container_type.uid in visited:
            return

        visited.add(container_type.uid)
        for prop in self.container_type.attributes:
            if not any(p.attribute.uid == prop.uid for p in self.properties):
                self.properties.append(Property(attribute=prop, value=None))

        for child_ct in self.container_type.children:
            if child_ct in visited:
                continue
            child_container = Container(
                container_type=child_ct, parent=self, _visited_children=visited, _max_depth=max_depth - 1
            )
            self.children.append(child_container)
