from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import Column, ForeignKey, Table
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.orm import Mapped, mapped_collection, mapped_column, relationship

if TYPE_CHECKING:
    from recap.models.attribute import AttributeTemplate
    from recap.models.process import ResourceAssignment

from .base import Base


def _reject_new(key, _value):
    raise KeyError(
        f"{key!r} is not a valid AttributeValue for this Parameter -"
        "keys are fixed by the template"
    )


class Property(Base):  # , AttributeValueMixin):
    __tablename__ = "property"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    resource_id: Mapped[UUID] = mapped_column(ForeignKey("resource.id"), nullable=False)
    resource: Mapped["Resource"] = relationship(back_populates="properties")

    attribute_template_id: Mapped[UUID] = mapped_column(
        ForeignKey("attribute_template.id")
    )
    template: Mapped["AttributeTemplate"] = relationship("AttributeTemplate")

    _values = relationship(
        "AttributeValue",
        collection_class=mapped_collection(lambda av: av.template.name),
        back_populates="property",
        cascade="all, delete-orphan",
    )

    values = association_proxy(
        "_values",
        "value",
        creator=_reject_new,
    )

    def __init__(self, *args, **kwargs):
        from .attribute import AttributeValue  # noqa

        template: AttributeTemplate = kwargs.get("template")
        super().__init__(*args, **kwargs)
        for vt in template.value_templates:
            av = AttributeValue(template=vt, property=self)
            av.set_value(vt.default_value)


resource_template_type_association = Table(
    "resource_template_type_association",
    Base.metadata,
    Column(
        "resource_template_id", ForeignKey("resource_template.id"), primary_key=True
    ),
    Column("resource_type_id", ForeignKey("resource_type.id"), primary_key=True),
)


class ResourceTemplate(Base):
    __tablename__ = "resource_template"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(nullable=True)

    types: Mapped[list["ResourceType"]] = relationship(
        "ResourceType",
        secondary=resource_template_type_association,
        back_populates="resource_templates",
    )
    parent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("resource_template.id"), nullable=True
    )
    parent: Mapped["ResourceTemplate"] = relationship(
        "ResourceTemplate", back_populates="children", remote_side=[id]
    )

    children: Mapped[list["ResourceTemplate"]] = relationship(
        "ResourceTemplate", back_populates="parent"
    )

    attribute_templates: Mapped[list["AttributeTemplate"]] = relationship(
        "AttributeTemplate",
        back_populates="resource_templates",
        secondary="resource_template_attribute_association",
    )


class ResourceType(Base):
    __tablename__ = "resource_type"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)
    resource_templates: Mapped[list[ResourceTemplate]] = relationship(
        "ResourceTemplate",
        secondary=resource_template_type_association,
        back_populates="types",
    )


class Resource(Base):
    __tablename__ = "resource"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(unique=True, nullable=True)
    ref_name: Mapped[str | None] = mapped_column(nullable=True)

    resource_template_id: Mapped[UUID] = mapped_column(
        ForeignKey("resource_template.id"), nullable=True
    )
    template: Mapped["ResourceTemplate"] = relationship()

    parent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("resource.id"), nullable=True
    )
    parent: Mapped["Resource"] = relationship(
        "Resource", back_populates="children", remote_side=[id]
    )

    children: Mapped[list["Resource"]] = relationship(
        "Resource", back_populates="parent"
    )

    properties = relationship(
        "Property",
        collection_class=mapped_collection(lambda p: p.template.name),
        back_populates="resource",
        cascade="all, delete-orphan",
    )

    assignments: Mapped[list["ResourceAssignment"]] = relationship(
        "ResourceAssignment", back_populates="resource", cascade="all, delete-orphan"
    )

    def __init__(
        self,
        *args,
        _init_children: bool = True,
        _visited_children: set[UUID] | None = None,
        _max_depth: int = 10,
        **kwargs,
    ):
        resource_template = kwargs.get("template")
        super().__init__(*args, **kwargs)

        if resource_template and _init_children:
            self._initialize_from_resource_template(
                resource_template, _visited_children, _max_depth
            )

    def _initialize_from_resource_template(
        self,
        resource_template: ResourceTemplate | None = None,
        visited: set[UUID] | None = None,
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
        for prop in self.template.attribute_templates:
            if not any(p.template.id == prop.id for name, p in self.properties.items()):
                self.properties[prop.name] = Property(template=prop)

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
