from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    Column,
    ForeignKey,
    Table,
    UniqueConstraint,
    event,
    func,
    inspect,
    select,
)
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.orm import Mapped, mapped_collection, mapped_column, relationship

from recap.db.process import ResourceAssignment
from recap.utils.general import make_slug

if TYPE_CHECKING:
    from recap.db.attribute import AttributeGroupTemplate

from .base import Base, TimestampMixin

# Sentinel for root ResourceTemplate
ROOT_RESOURCE_TEMPLATE_ID = UUID("00000000-0000-0000-0000-000000000001")


def _reject_new(key, _value):
    raise KeyError(
        f"{key!r} is not a valid AttributeValue for this Parameter -"
        "keys are fixed by the template"
    )


class Property(TimestampMixin, Base):
    __tablename__ = "property"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    resource_id: Mapped[UUID] = mapped_column(ForeignKey("resource.id"), nullable=False)
    resource: Mapped["Resource"] = relationship(back_populates="properties")

    attribute_group_template_id: Mapped[UUID] = mapped_column(
        ForeignKey("attribute_group_template.id")
    )
    template: Mapped["AttributeGroupTemplate"] = relationship("AttributeGroupTemplate")

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

        template: AttributeGroupTemplate = kwargs.get("template")
        super().__init__(*args, **kwargs)
        for vt in template.attribute_templates:
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


class ResourceTemplate(TimestampMixin, Base):
    __tablename__ = "resource_template"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(nullable=False)
    slug: Mapped[str] = mapped_column(nullable=True)
    version: Mapped[str] = mapped_column(nullable=False, default="1.0")

    types: Mapped[list["ResourceType"]] = relationship(
        "ResourceType",
        secondary=resource_template_type_association,
    )
    parent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("resource_template.id"), nullable=True
    )
    parent: Mapped["ResourceTemplate"] = relationship(
        "ResourceTemplate", back_populates="children", remote_side=[id]
    )

    children: Mapped[dict[str, "ResourceTemplate"]] = relationship(
        "ResourceTemplate",
        back_populates="parent",
        collection_class=mapped_collection(lambda c: c.name),
    )

    attribute_group_templates: Mapped[list["AttributeGroupTemplate"]] = relationship(
        "AttributeGroupTemplate",
        back_populates="resource_template",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint(
            "parent_id",
            "name",
            "version",
            name="uq_resource_template_parent_name_version",
        ),
    )


# --- Keep slug always in sync with name ---
@event.listens_for(ResourceTemplate, "before_insert", propagate=True)
def _before_insert_resource_template(mapper, connection, target: ResourceTemplate):
    target.slug = make_slug(target.name)
    # Default top-level templates to the root sentinel to avoid NULL parent collisions
    if target.name != "__root__" and target.parent_id is None:
        target.parent_id = ROOT_RESOURCE_TEMPLATE_ID


@event.listens_for(ResourceTemplate, "before_update", propagate=True)
def _before_update_resource_template(mapper, connection, target: ResourceTemplate):
    target.slug = make_slug(target.name)


class ResourceType(TimestampMixin, Base):
    __tablename__ = "resource_type"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)


class Resource(TimestampMixin, Base):
    __tablename__ = "resource"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(nullable=False)
    slug: Mapped[str | None] = mapped_column(nullable=True, index=True)
    active: Mapped[bool] = mapped_column(nullable=False, default=True)
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
    children: Mapped[dict[str, "Resource"]] = relationship(
        "Resource",
        back_populates="parent",
        collection_class=mapped_collection(lambda c: c.name),
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
    campaigns = association_proxy("assignments", "process_run.campaign")

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
        for prop in self.template.attribute_group_templates:
            if not any(p.template.id == prop.id for name, p in self.properties.items()):
                self.properties[prop.name] = Property(template=prop)

        for child_ct in self.template.children.values():
            if child_ct.id is not None and child_ct.id in visited:
                continue
            Resource(
                name=child_ct.name,
                template=child_ct,
                parent=self,
                _visited_children=visited,
                _max_depth=max_depth - 1,
            )

    __table_args__ = (
        UniqueConstraint(
            "parent_id",
            "name",
            name="uq_resource_parent_name",
        ),
    )


# --- Keep slug always in sync with name ---
@event.listens_for(Resource, "before_insert", propagate=True)
def _before_insert(mapper, connection, target: Resource):
    target.slug = make_slug(target.name)
    # Set active as True
    if target.active is None:
        target.active = True

    # Update all other copies of the resource to active = False
    if target.active and target.name:
        tbl = Resource.__table__
        connection.execute(
            tbl.update().where(tbl.c.name == target.name).values(active=False)
        )


def _descendant_templates_cte(template_id):
    rt = ResourceTemplate.__table__
    base = select(rt.c.id).where(rt.c.id == template_id).cte(recursive=True)
    descendants = select(rt.c.id).where(rt.c.parent_id == base.c.id)
    return base.union_all(descendants)


@event.listens_for(ResourceTemplate, "before_update", propagate=True)
@event.listens_for(ResourceTemplate, "before_delete", propagate=True)
def _guard_resource_template(mapper, connection, target: ResourceTemplate):
    state = inspect(target)
    column_changes = [
        col.key
        for col in mapper.column_attrs
        if state.attrs[col.key].history.has_changes()
        and col.key not in {"modified_date"}
    ]
    if not column_changes:
        return
    cte = _descendant_templates_cte(target.id)
    res_tbl = Resource.__table__
    count_stmt = (
        select(func.count())
        .select_from(res_tbl)
        .where(res_tbl.c.resource_template_id.in_(select(cte.c.id)))
    )
    cnt = connection.scalar(count_stmt)
    if cnt and cnt > 0:
        raise ValueError(
            "Cannot modify or delete a resource template that already has resources. "
            "Create a new template version instead."
        )


@event.listens_for(Resource, "before_update", propagate=True)
def _before_update(mapper, connection, target: Resource):
    target.slug = make_slug(target.name)
    # If this resource is set to active, set others as inactive
    if target.active and target.name:
        tbl = Resource.__table__
        connection.execute(
            tbl.update()
            .where(tbl.c.name == target.name, tbl.c.id != target.id)
            .values(active=False)
        )
