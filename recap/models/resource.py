from typing import List, Optional, Set, TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
    relationship,
)

from .attribute import (
    Attribute,
    AttributeValueMixin,
    resource_template_attribute_association,
)

if TYPE_CHECKING:
    from recap.models.process import ResourceAssignment
from .base import Base


class Property(Base, AttributeValueMixin):
    __tablename__ = "property"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    resource_id: Mapped[UUID] = mapped_column(ForeignKey("resource.id"), nullable=False)
    resource: Mapped["Resource"] = relationship(back_populates="properties")

    def __init__(self, *args, **kwargs):
        value = kwargs.pop("value", None)
        attribute = kwargs.get("attribute")
        super().__init__(*args, **kwargs)
        if value is None:
            value = attribute.default_value
        self.set_value(value)


class ResourceTemplate(Base):
    __tablename__ = "resource_template"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)
    ref_name: Mapped[str] = mapped_column(nullable=False)

    type_id: Mapped[UUID] = mapped_column(
        ForeignKey("resource_type.id"), nullable=False
    )
    type: Mapped["ResourceType"] = relationship(back_populates="resource_templates")

    parent_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("resource_template.id"), nullable=True
    )
    parent: Mapped["ResourceTemplate"] = relationship(
        "ResourceTemplate", back_populates="children", remote_side=[id]
    )

    children: Mapped[List["ResourceTemplate"]] = relationship(
        "ResourceTemplate", back_populates="parent"
    )

    attributes: Mapped[List["Attribute"]] = relationship(
        back_populates="resource_templates",
        secondary=resource_template_attribute_association,
    )


class ResourceType(Base):
    __tablename__ = "resource_type"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)
    resource_templates: Mapped[List[ResourceTemplate]] = relationship(
        back_populates="type"
    )


class Resource(Base):
    __tablename__ = "resource"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(unique=True, nullable=True)
    ref_name: Mapped[Optional[str]] = mapped_column(nullable=True)

    resource_template_id: Mapped[UUID] = mapped_column(
        ForeignKey("resource_template.id"), nullable=True
    )
    template: Mapped["ResourceTemplate"] = relationship()

    parent_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("resource.id"), nullable=True
    )
    parent: Mapped["Resource"] = relationship(
        "Resource", back_populates="children", remote_side=[id]
    )

    children: Mapped[List["Resource"]] = relationship(
        "Resource", back_populates="parent"
    )

    properties: Mapped[List["Property"]] = relationship(
        "Property", back_populates="resource"
    )

    assignments: Mapped[list["ResourceAssignment"]] = relationship(
        "ResourceAssignment", back_populates="resource", cascade="all, delete-orphan"
    )

    def __init__(
        self,
        *args,
        _init_children: bool = True,
        _visited_children: Optional[Set[UUID]] = None,
        _max_depth: int = 10,
        **kwargs,
    ):
        resource_template = kwargs.get("template", None)
        super().__init__(*args, **kwargs)

        if resource_template and _init_children:
            self._initialize_from_resource_template(
                resource_template, _visited_children, _max_depth
            )

    def _initialize_from_resource_template(
        self,
        resource_template: Optional[ResourceTemplate] = None,
        visited: Optional[Set[UUID]] = None,
        max_depth: int = 10,
    ):
        """
        Automatically initialize resource from resource_template
        - Use visited to avoid using the same resource_template to prevent cycles
        - max_depth should prevent too many recursions
        - Only add properties if not present
        """
        if not resource_template:
            return

        if max_depth <= 0:
            return

        if visited is None:
            visited = set()

        if resource_template.id in visited:
            return

        visited.add(resource_template.id)
        for prop in self.template.attributes:
            if not any(p.attribute.id == prop.id for p in self.properties):
                self.properties.append(Property(attribute=prop, value=None))

        for child_ct in self.template.children:
            if child_ct in visited:
                continue
            child_resource = Resource(
                template=child_ct,
                parent=self,
                _visited_children=visited,
                _max_depth=max_depth - 1,
            )
            self.children.append(child_resource)

        visited.add(resource_template.id)
        for prop in self.template.attributes:
            if not any(p.attribute.id == prop.id for p in self.properties):
                self.properties.append(Property(attribute=prop, value=None))

        for child_ct in self.template.children:
            if child_ct in visited:
                continue
            child_resource = Resource(
                template=child_ct,
                parent=self,
                _visited_children=visited,
                _max_depth=max_depth - 1,
            )
            self.children.append(child_resource)
