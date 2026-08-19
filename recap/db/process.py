import typing
from collections import namedtuple
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Enum,
    ForeignKey,
    Index,
    UniqueConstraint,
    event,
    inspect,
)
from sqlalchemy.ext import associationproxy
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import (
    Mapped,
    Session,
    attribute_mapped_collection,
    mapped_collection,
    mapped_column,
    object_session,
    relationship,
    validates,
)

from recap.db.namespace import Namespace
from recap.db.step import Step, StepTemplate, StepTemplateEdge
from recap.exceptions import DuplicateResourceError
from recap.lifecycle import LifecycleStatus, validate_transition
from recap.utils.general import Direction, make_slug

from .base import Base, RevisionedLifecycleMixin, TimestampMixin

if typing.TYPE_CHECKING:
    from recap.db.resource import Resource, ResourceType

AssignedResource = namedtuple("AssignedResource", ["slot", "resource"])


class ProcessTemplate(RevisionedLifecycleMixin, TimestampMixin, Base):
    __tablename__ = "process_template"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    namespace_id: Mapped[UUID] = mapped_column(
        ForeignKey("namespace.id"), nullable=False
    )
    namespace: Mapped[Namespace] = relationship()
    name: Mapped[str] = mapped_column(nullable=False)
    version: Mapped[str] = mapped_column(nullable=False)
    labels: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON), nullable=False, default=list
    )
    step_templates: Mapped[dict[str, "StepTemplate"]] = relationship(
        back_populates="process_template",
        collection_class=mapped_collection(lambda st: st.name),
    )
    edges: Mapped["StepTemplateEdge"] = relationship(
        "StepTemplateEdge",
        primaryjoin=id == StepTemplateEdge.process_template_id,
        cascade="all, delete-orphan",
    )
    resource_slots: Mapped[list["ResourceSlot"]] = relationship(
        "ResourceSlot", back_populates="process_template"
    )
    __table_args__ = (
        UniqueConstraint(
            "namespace_id",
            "name",
            "version",
            name="uq_process_template_namespace_name_version",
        ),
    )

    @validates("labels")
    def _normalize_labels(self, key, labels):
        return [make_slug(label) for label in labels]

    def activate(self):
        validate_transition(
            self.status or LifecycleStatus.MUTABLE, LifecycleStatus.ACTIVE
        )
        self.status = LifecycleStatus.ACTIVE

    def archive(self):
        validate_transition(
            self.status or LifecycleStatus.MUTABLE, LifecycleStatus.ARCHIVED
        )
        self.status = LifecycleStatus.ARCHIVED


class ResourceSlot(TimestampMixin, Base):
    __tablename__ = "resource_slot"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column()
    process_template_id: Mapped[UUID] = mapped_column(
        ForeignKey("process_template.id"), nullable=False
    )
    process_template: Mapped[ProcessTemplate] = relationship(
        ProcessTemplate, back_populates="resource_slots"
    )
    resource_type_id: Mapped[UUID] = mapped_column(
        ForeignKey("resource_type.id"), nullable=False
    )
    resource_type: Mapped["ResourceType"] = relationship("ResourceType")
    direction: Mapped[Direction] = mapped_column(
        Enum(Direction, name="direction_enum"), nullable=False
    )
    required: Mapped[bool] = mapped_column(default=True, server_default="1")

    __table_args__ = (
        UniqueConstraint(
            "process_template_id", "name", name="uq_process_template_name"
        ),
    )


class ProcessRun(RevisionedLifecycleMixin, TimestampMixin, Base):
    __tablename__ = "process_run"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    namespace_id: Mapped[UUID] = mapped_column(
        ForeignKey("namespace.id"), nullable=False
    )
    namespace: Mapped[Namespace] = relationship()
    name: Mapped[str] = mapped_column(nullable=False)
    description: Mapped[str] = mapped_column(unique=False, nullable=False)
    copied_from_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("process_run.id"), nullable=True
    )
    copied_from: Mapped["ProcessRun | None"] = relationship(
        "ProcessRun", foreign_keys=[copied_from_id], remote_side=[id]
    )

    process_template_id: Mapped[UUID] = mapped_column(
        ForeignKey("process_template.id"), nullable=False
    )
    template: Mapped[ProcessTemplate] = relationship()

    assignments: Mapped[dict["ResourceSlot", "ResourceAssignment"]] = relationship(
        "ResourceAssignment",
        primaryjoin="and_(ProcessRun.id==ResourceAssignment.process_run_id, ResourceAssignment.step_id==None)",
        back_populates="process_run",
        cascade="all, delete-orphan",
        collection_class=attribute_mapped_collection("resource_slot"),
    )
    resources = associationproxy.association_proxy(
        "assignments",
        "resource",
        creator=lambda res_slot, resource: ResourceAssignment(
            resource_slot=res_slot, resource=resource
        ),
    )
    steps: Mapped[dict[str, "Step"]] = relationship(
        back_populates="process_run",
        collection_class=mapped_collection(lambda s: s.name),
    )
    __table_args__ = (
        Index(
            "uq_process_run_namespace_name_root",
            "namespace_id",
            "name",
            unique=True,
            sqlite_where=(copied_from_id.is_(None)),
        ),
    )

    def __init__(self, *args, **kwargs):
        template: ProcessTemplate | None = kwargs.get("template")
        if template is None:
            raise ValueError("Missing template for ProcessRun")
        step_templates = list(template.step_templates.values())
        super().__init__(*args, **kwargs)
        for step_template in step_templates:
            step = Step(template=step_template, name=step_template.name)
            # Attach via the mapped collection to ensure the dict key is derived
            # from the final name instead of a default/None during construction.
            self.steps[step.name] = step

    @validates("assignments")
    def _check_assignment(self, key, assignment: "ResourceAssignment"):  # noqa
        if assignment.process_run is None:
            assignment.process_run = self
        sess = object_session(self)
        if sess is not None and assignment not in sess:
            sess.add(assignment)
        slot = assignment.resource_slot
        resource = assignment.resource

        if slot.process_template is not self.template:
            raise ValueError(
                f"Slot {slot.name} does not belong to process template {self.template.name}"
            )

        # Resource must advertise the slot's type via its template's types
        resource_type_ids = {rt.id for rt in resource.template.types}
        if slot.resource_type_id not in resource_type_ids:
            raise ValueError(
                f"Resource {resource.name} does not match required type for slot {slot.name}"
            )

        # Slot must not already be used in this run
        for existing in self.assignments.values():
            if existing is assignment:
                continue
            if existing.resource_slot_id == slot.id:
                raise ValueError(
                    f"Slot {slot.name} is already occupied in run {self.id}"
                )

        # Auto-populate step-level assignments for steps bound to this slot.
        # Explicit step assignments remain untouched and take precedence.
        for step in self.steps.values():
            bound_slot_ids = {
                binding.resource_slot_id for binding in step.template.bindings.values()
            }
            if slot.id not in bound_slot_ids:
                continue
            if slot.id in step.assignments:
                continue

            if sess is not None:
                with sess.no_autoflush:
                    step_assignment = ResourceAssignment(
                        process_run=self,
                        resource_slot=slot,
                        resource_slot_id=slot.id,
                        step=step,
                        step_id=step.id,
                        resource=resource,
                    )
            else:
                step_assignment = ResourceAssignment(
                    process_run=self,
                    resource_slot=slot,
                    resource_slot_id=slot.id,
                    step=step,
                    step_id=step.id,
                    resource=resource,
                )
            if sess is not None and step_assignment not in sess:
                sess.add(step_assignment)

        return assignment

    @property
    def assigned_resources(self):
        ar = []
        for resource_slot, resource in self.resources.items():
            ar.append(AssignedResource(slot=resource_slot, resource=resource))
        return ar

    def finalize(self):
        validate_transition(
            self.status or LifecycleStatus.MUTABLE, LifecycleStatus.ACTIVE
        )
        self.status = LifecycleStatus.ACTIVE

    def archive(self):
        validate_transition(
            self.status or LifecycleStatus.MUTABLE, LifecycleStatus.ARCHIVED
        )
        self.status = LifecycleStatus.ARCHIVED


@event.listens_for(ProcessTemplate, "before_update", propagate=True)
@event.listens_for(ProcessRun, "before_update", propagate=True)
def _guard_process_namespace_id(mapper, connection, target):
    if inspect(target).attrs.namespace_id.history.has_changes():
        raise ValueError("namespace_id is immutable")


class ResourceAssignment(TimestampMixin, Base):
    __tablename__ = "resource_assignment"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    process_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("process_run.id"), nullable=False
    )
    resource_slot_id: Mapped[UUID] = mapped_column(
        ForeignKey("resource_slot.id"), nullable=False
    )
    step_id: Mapped[UUID | None] = mapped_column(ForeignKey("step.id"), nullable=True)
    resource_id: Mapped[UUID] = mapped_column(ForeignKey("resource.id"), nullable=False)

    process_run: Mapped["ProcessRun"] = relationship("ProcessRun")
    resource_slot: Mapped["ResourceSlot"] = relationship()
    resource: Mapped["Resource"] = relationship(
        "Resource", back_populates="assignments"
    )
    step: Mapped["Step | None"] = relationship("Step", back_populates="assignments")

    @validates("resource")
    def _check_resource_namespace_uniqueness(self, key, resource: "Resource"):
        if self.process_run:
            if self.process_run_id is None or self.resource_slot_id is None:
                return resource
            namespace_id = self.process_run.namespace_id
            this_step_id = self.step_id or (
                self.step.id if self.step is not None else None
            )
            this_run_id = self.process_run_id or (
                self.process_run.id if self.process_run is not None else None
            )
            this_slot_id = self.resource_slot_id or (
                self.resource_slot.id if self.resource_slot is not None else None
            )
            for assignment in resource.assignments:
                if assignment is self:
                    continue
                other_run_id = assignment.process_run_id or (
                    assignment.process_run.id
                    if assignment.process_run is not None
                    else None
                )
                other_slot_id = assignment.resource_slot_id or (
                    assignment.resource_slot.id
                    if assignment.resource_slot is not None
                    else None
                )
                if other_run_id is None or other_slot_id is None:
                    continue
                other_step_id = assignment.step_id or (
                    assignment.step.id if assignment.step is not None else None
                )
                # Run-level and step-level assignment pair for the same run/slot
                # represent one logical assignment; allow this combination.
                if (
                    this_run_id is not None
                    and this_slot_id is not None
                    and other_run_id == this_run_id
                    and other_slot_id == this_slot_id
                    and ((this_step_id is None) != (other_step_id is None))
                ):
                    continue
                if (
                    assignment.process_run
                    and assignment.process_run.namespace_id == namespace_id
                    and assignment.resource.parent_id == resource.parent_id
                    and other_step_id == this_step_id
                ):
                    raise DuplicateResourceError(
                        resource.name,
                        assignment.process_run.namespace.path,
                        assignment.process_run.name,
                        assignment.step.name if assignment.step else None,
                    )
        return resource

    __table_args__ = (
        UniqueConstraint(
            "process_run_id", "resource_slot_id", "step_id", name="uq_run_slot_step"
        ),
    )


def _relationship_value(obj, name):
    value = getattr(obj, name, None)
    if value is not None:
        return value
    state = inspect(obj)
    if name not in state.attrs:
        return None
    deleted = state.attrs[name].history.deleted
    return deleted[0] if deleted else None


def _resource_root(resource):
    current = resource
    while (parent := _relationship_value(current, "parent")) is not None:
        current = parent
    return current


def _resource_template_root(template):
    from recap.db.resource import ROOT_RESOURCE_TEMPLATE_ID

    current = template
    while (
        parent := _relationship_value(current, "parent")
    ) is not None and parent.id != ROOT_RESOURCE_TEMPLATE_ID:
        current = parent
    return current


def _aggregate_root(session, obj):  # noqa: C901
    from recap.db.attribute import (
        AttributeGroupTemplate,
        AttributeTemplate,
        AttributeValue,
    )
    from recap.db.resource import Property, Resource, ResourceTemplate
    from recap.db.step import (
        Parameter,
        Step,
        StepTemplateResourceSlotBinding,
    )

    if isinstance(obj, ProcessTemplate | ProcessRun):
        return obj
    if isinstance(obj, StepTemplate):
        return _relationship_value(obj, "process_template")
    if isinstance(obj, ResourceSlot):
        return _relationship_value(obj, "process_template")
    if isinstance(obj, StepTemplateResourceSlotBinding):
        step_template = _relationship_value(obj, "step_template")
        return _aggregate_root(session, step_template) if step_template else None
    if isinstance(obj, ResourceTemplate):
        return _resource_template_root(obj)
    if isinstance(obj, AttributeGroupTemplate):
        owner = _relationship_value(obj, "resource_template") or _relationship_value(
            obj, "step_template"
        )
        return _aggregate_root(session, owner) if owner else None
    if isinstance(obj, AttributeTemplate):
        group = _relationship_value(obj, "attribute_group_template")
        return _aggregate_root(session, group) if group else None
    if isinstance(obj, Resource):
        return _resource_root(obj)
    if isinstance(obj, Property):
        resource = _relationship_value(obj, "resource")
        return _resource_root(resource) if resource else None
    if isinstance(obj, AttributeValue):
        prop = _relationship_value(obj, "property")
        if prop is not None:
            return _aggregate_root(session, prop)
        parameter = _relationship_value(obj, "parameter")
        return _aggregate_root(session, parameter) if parameter else None
    if isinstance(obj, Parameter):
        step = _relationship_value(obj, "step")
        return _aggregate_root(session, step) if step else None
    if isinstance(obj, Step):
        return _relationship_value(obj, "process_run")
    return None


def _source_status(root):
    history = inspect(root).attrs.status.history
    return history.deleted[0] if history.deleted else root.status


def _validate_status_change(root):
    history = inspect(root).attrs.status.history
    if history.has_changes() and history.deleted:
        validate_transition(history.deleted[0], root.status)


@event.listens_for(Session, "before_flush")
def _enforce_provenance_lifecycle(session, flush_context, instances):  # noqa: C901
    from recap.db.resource import Resource

    # Stable references activate their provenance definitions/instances in the
    # same unit of work. Freeze checks below use the persisted source status.
    for obj in tuple(session.new):
        if isinstance(obj, ProcessRun) and obj.template is not None:
            obj.template.activate()
        elif isinstance(obj, Resource) and obj.template is not None:
            _resource_template_root(obj.template).activate()
        elif isinstance(obj, ResourceAssignment) and obj.resource is not None:
            _resource_root(obj.resource).activate()

    lifecycle_roots = {
        obj
        for obj in session.dirty
        if isinstance(obj, ProcessTemplate | ProcessRun | Resource)
    }
    from recap.db.resource import ResourceTemplate

    lifecycle_roots.update(
        obj for obj in session.dirty if isinstance(obj, ResourceTemplate)
    )
    for root in lifecycle_roots:
        _validate_status_change(root)

    changed = set(session.new) | set(session.dirty) | set(session.deleted)
    for obj in changed:
        root = _aggregate_root(session, obj)
        if root is None or root in session.new:
            continue
        source_status = _source_status(root)
        if source_status is LifecycleStatus.MUTABLE:
            continue
        if obj is root:
            state = inspect(obj)
            changed_keys = {
                attr.key
                for attr in state.attrs
                if attr.history.has_changes()
                and attr.key not in {"assignments", "status", "modified_date"}
            }
            if not changed_keys and obj not in session.deleted:
                continue
        if isinstance(root, ProcessTemplate):
            raise ValueError("Cannot mutate an active process template aggregate")
        if isinstance(root, ProcessRun):
            raise ValueError("Cannot mutate a finalized process run aggregate")
        if isinstance(root, ResourceTemplate):
            raise ValueError("Cannot mutate an active resource template aggregate")
        raise ValueError("Cannot mutate an active resource aggregate")
