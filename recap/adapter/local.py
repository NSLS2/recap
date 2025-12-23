import warnings
from contextlib import contextmanager
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, create_model
from sqlalchemy import Boolean, Float, Integer, Select, String, cast, insert, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session, aliased, selectinload
from sqlalchemy.sql import and_, or_
from sqlalchemy.sql.functions import count

from recap.adapter import Backend, UnitOfWork
from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate, AttributeValue
from recap.db.base import Base
from recap.db.campaign import Campaign
from recap.db.exceptions import ValidationError
from recap.db.process import (
    Direction,
    ProcessRun,
    ProcessTemplate,
    ResourceAssignment,
    ResourceSlot,
)
from recap.db.resource import (
    Property,
    Resource,
    ResourceTemplate,
    ResourceType,
    resource_template_type_association,
)
from recap.db.step import (
    Parameter,
    Step,
    StepTemplate,
    StepTemplateResourceSlotBinding,
)
from recap.dsl.query import QuerySpec, SchemaT
from recap.schemas.attribute import AttributeGroupRef, AttributeTemplateSchema
from recap.schemas.process import (
    CampaignSchema,
    ProcessRunRef,
    ProcessRunSchema,
    ProcessTemplateRef,
    ProcessTemplateSchema,
)
from recap.schemas.resource import (
    ResourceRef,
    ResourceSchema,
    ResourceSlotSchema,
    ResourceTemplateRef,
    ResourceTemplateSchema,
    ResourceTypeSchema,
)
from recap.schemas.step import StepSchema, StepTemplateRef, StepTemplateSchema
from recap.utils.database import get_or_create, load_single
from recap.utils.dsl import (
    AliasMixin,
    build_param_values_model,
    resolve_path,
)
from recap.utils.general import make_slug, to_json_compatible

SCHEMA_MODEL_MAPPING: dict[type[BaseModel], type[Base]] = {
    CampaignSchema: Campaign,
    ResourceTemplateSchema: ResourceTemplate,
    ResourceTemplateRef: ResourceTemplate,
    ProcessRunRef: ProcessRun,
    ProcessRunSchema: ProcessRun,
    ProcessTemplateRef: ProcessTemplate,
    ResourceSchema: Resource,
    ResourceRef: Resource,
    ProcessTemplateSchema: ProcessTemplate,
}


class SQLUnitOfWork(UnitOfWork):
    def __init__(self, backend: "LocalBackend", session: Session, tx):
        self._backend = backend
        self._session = session
        self._tx = tx

    def commit(self, clear_session=True):
        self._tx.commit()
        if clear_session:
            self._backend._clear_session(self._session)

    def end_session(self):
        self._backend._clear_session(self._session)

    def rollback(self):
        self._tx.rollback()
        self._backend._clear_session(self._session)


class LocalBackend(Backend):
    def __init__(self, session_factory):
        self._session_factory = session_factory
        self._session: Session | None = None

    def _get_session(self) -> Session:
        if self._session is None:
            raise RuntimeError("No active session; call begin() first")
        return self._session

    @property
    def session(self) -> Session:
        return self._get_session()

    def _clear_session(self, session: Session):
        if self._session is session:
            session.close()
            self._session = None

    def close(self):
        """Close any active session if it is still open."""
        if self._session is not None:
            self._session.close()
            self._session = None

    @contextmanager
    def _session_scope(self):
        """Yield a session, closing it if we had to create one."""
        if self._session is not None:
            yield self._session
            return
        session = self._session_factory()
        try:
            yield session
        finally:
            session.close()

    def begin(self) -> UnitOfWork:
        if self._session is not None:
            raise RuntimeError(
                "An active session already exists; nested begin() calls are not supported"
            )
        session = self._session_factory()
        tx = session.begin()
        self._session = session
        return SQLUnitOfWork(self, session, tx)

    def create_campaign(
        self,
        name: str,
        proposal: str,
        saf: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> CampaignSchema:
        self._campaign = Campaign(
            name=name,
            proposal=str(proposal),
            saf=saf,
            metadata=metadata,
        )
        self.session.add(self._campaign)
        self.session.flush()
        return CampaignSchema.model_validate(self._campaign)

    def set_campaign(self, id: UUID) -> CampaignSchema:
        statement = select(Campaign).filter_by(id=id)
        self._campaign = self.session.execute(statement).scalar_one_or_none()
        if self._campaign is None:
            raise ValueError(f"Campaign with ID {id} not found")
        return CampaignSchema.model_validate(self._campaign)

    def create_process_template(
        self, name: str, version: str
    ) -> ProcessTemplateRef | None:
        template, created = get_or_create(
            self.session, ProcessTemplate, {"name": name, "version": version}, {}
        )
        if created:
            return ProcessTemplateRef.model_validate(template)

    def get_process_template(
        self,
        name: str | None,
        version: str | None,
        expand: bool = False,
        id: UUID | str | None = None,
    ) -> ProcessTemplateRef | ProcessTemplateSchema:
        statement = select(ProcessTemplate)
        if name:
            statement = statement.where(ProcessTemplate.name == name)
        if version:
            statement = statement.where(ProcessTemplate.version == version)
        if isinstance(id, str):
            id = UUID(id)
        if id:
            statement = statement.where(ProcessTemplate.id == id)
        if not name and not id:
            raise ValueError("name or id required to fetch ProcessTemplate")
        if expand:
            statement = statement.options(
                selectinload(ProcessTemplate.step_templates),
                selectinload(ProcessTemplate.resource_slots),
            )
        process_template = load_single(self.session, statement, label="ProcessTemplate")
        if expand:
            return ProcessTemplateSchema.model_validate(process_template)
        return ProcessTemplateRef.model_validate(process_template)

    def add_resource_slot(
        self,
        name: str,
        resource_type: str,
        direction: Direction,
        process_template_ref: ProcessTemplateRef,
        create_resource_type=False,
    ) -> ResourceSlotSchema:
        rt = self.session.execute(
            select(ResourceType).filter_by(name=resource_type)
        ).scalar_one_or_none()
        if rt is None:
            if not create_resource_type:
                raise ValueError(
                    f"Could not find resource_type named {resource_type}. Use create_resource_type=True to create one"
                )
            else:
                rt = ResourceType(name=resource_type)
                self.session.add(rt)
        slot, _ = get_or_create(
            self.session,
            ResourceSlot,
            where={"process_template_id": process_template_ref.id, "name": name},
            defaults={"resource_type": rt, "direction": direction},
        )
        if slot.resource_type_id != rt.id and slot.direction != direction:
            raise ValueError(
                f"ResourceSlot {name} already exists with different type/direction"
            )
        return ResourceSlotSchema.model_validate(slot)

    def add_step(
        self, name: str, process_template_ref: ProcessTemplateRef
    ) -> StepTemplateRef:
        step_template = StepTemplate(
            name=name, process_template_id=process_template_ref.id
        )
        self.session.add(step_template)
        self.session.flush()
        return StepTemplateRef.model_validate(step_template)

    def bind_slot(
        self,
        role: str,
        slot_name: str,
        process_template_ref: ProcessTemplateRef,
        step_template_ref: StepTemplateRef,
    ) -> ResourceSlotSchema | None:
        slot = self.session.scalars(
            select(ResourceSlot).where(
                ResourceSlot.process_template_id == process_template_ref.id,
                ResourceSlot.name == slot_name,
            )
        ).one_or_none()
        if slot is None:
            warnings.warn(
                f"Did not find ResourceSlot named {slot_name}. Nothing added",
                stacklevel=2,
            )
            return
        step_template = self.session.scalars(
            select(StepTemplate).where(StepTemplate.id == step_template_ref.id)
        ).one()
        step_template.resource_slots[role] = slot
        return ResourceSlotSchema.model_validate(slot)

    def add_attr_group(
        self, group_name: str, template_ref: StepTemplateRef | ResourceTemplateRef
    ) -> AttributeGroupRef:
        filter_params: dict[str, Any] = {"name": group_name}
        if isinstance(template_ref, StepTemplateRef | StepTemplateSchema):
            filter_params["step_template_id"] = template_ref.id
        elif isinstance(template_ref, ResourceTemplateRef | ResourceTemplateSchema):
            filter_params["resource_template_id"] = template_ref.id
        results = list(
            self.session.scalars(
                select(AttributeGroupTemplate)
                .filter_by(**filter_params)
                .order_by(AttributeGroupTemplate.create_date)
            )
        )
        if results:
            if len(results) > 1:
                warnings.warn(
                    f"Multiple attribute groups named {group_name!r} exist for template "
                    f"{template_ref}; using the first existing group.",
                    stacklevel=2,
                )
            attr_group_template = results[0]
        else:
            attr_group_template = AttributeGroupTemplate(**filter_params)
            self.session.add(attr_group_template)
            self.session.flush()
        return AttributeGroupRef.model_validate(attr_group_template)

    def add_attribute(
        self,
        name: str,
        value_type: str,
        unit: str,
        default: Any,
        attribute_group_ref: AttributeGroupRef,
        metadata: dict[str, Any] | None = None,
    ) -> AttributeTemplateSchema:
        filter_params: dict[str, Any] = {
            "name": name,
            "value_type": value_type,
            "unit": unit,
            "default_value": default,
            "attribute_group_template_id": attribute_group_ref.id,
            "metadata_json": metadata or {},
        }
        attribute_template = self.session.execute(
            select(AttributeTemplate).filter_by(**filter_params)
        ).scalar_one_or_none()
        if attribute_template is None:
            attribute_template = AttributeTemplate(**filter_params)
            self.session.add(attribute_template)
            self.session.flush()

        return AttributeTemplateSchema.model_validate(attribute_template)

    def remove_attribute(self, name: str, attribute_group: AttributeGroupRef):
        attribute = self.session.execute(
            select(AttributeTemplate).filter_by(
                name=name, attribute_group_template_id=attribute_group.id
            )
        ).scalar_one_or_none()
        if attribute is None:
            warnings.warn(
                f"Property does not exist in group {attribute_group.name}: {name}",
                stacklevel=2,
            )
        self.session.delete(attribute)
        self.session.flush()

    def add_resource_types(self, type_names: list[str]) -> list[ResourceTypeSchema]:
        resource_type_schemas = []
        for type_name in type_names:
            where = {"name": type_name}
            resource_type, _ = get_or_create(self.session, ResourceType, where=where)
            resource_type_schemas.append(
                ResourceTypeSchema.model_validate(resource_type)
            )

        return resource_type_schemas

    def add_resource_template(
        self, name: str, types: list[ResourceTypeSchema], version: str = "1.0"
    ) -> ResourceTemplateRef:
        template = ResourceTemplate(
            name=name,
            version=version,
            # types=types,
        )
        self.session.add(template)
        self.session.flush()
        type_ids = [type.id for type in types]
        if type_ids:
            self.session.execute(
                insert(resource_template_type_association),
                [
                    {"resource_template_id": template.id, "resource_type_id": type_id}
                    for type_id in type_ids
                ],
            )
        return ResourceTemplateRef.model_validate(template)

    def add_child_resource_template(
        self,
        name: str,
        resource_types: list[ResourceTypeSchema],
        parent_resource_template: ResourceTemplateRef | ResourceTemplateSchema,
        version: str = "1.0",
    ) -> ResourceTemplateRef:
        stmt = select(ResourceType).where(
            ResourceType.id.in_([type.id for type in resource_types])
        )
        resource_type_results = self.session.scalars(stmt).all()

        template = ResourceTemplate(
            name=name,
            version=version,
            types=resource_type_results,
        )
        parent_template = self.session.get(
            ResourceTemplate, parent_resource_template.id
        )
        if parent_template is None:
            raise NoResultFound(
                f"Parent template: {parent_resource_template.name} with id {parent_resource_template.id} not found"
            )
        parent_template.children[template.name] = template
        self.session.add(template)
        self.session.flush()
        return ResourceTemplateRef.model_validate(template)

    def add_child_resources(
        self,
        parent_resource: ResourceSchema | ResourceRef,
        child_resources: list[ResourceSchema | ResourceRef],
    ):
        parent = self.session.get(Resource, parent_resource.id)
        children_stmt = select(Resource).where(
            Resource.id.in_([r.id for r in child_resources])
        )
        child_resources_results = self.session.scalars(children_stmt).all()
        if parent:
            for c in child_resources_results:
                parent.children[c.name] = c
        self.session.add(parent)
        self.session.flush()

    def get_resource_template(
        self,
        name: str | None,
        version: str | None = None,
        id: UUID | str | None = None,
        parent: ResourceTemplateRef | ResourceTemplate | None = None,
        expand: bool = False,
    ) -> ResourceTemplateRef | ResourceTemplateSchema:
        statement = select(ResourceTemplate)
        if name:
            statement = statement.where(ResourceTemplate.name == name)
        if version:
            statement = statement.where(ResourceTemplate.version == version)
        if not name and not id:
            raise ValueError("name or id required to fetch ResourceTemplate")
        if isinstance(id, str):
            id = UUID(id)
        if id:
            statement = statement.where(ResourceTemplate.id == id)
        if parent:
            statement = statement.where(ResourceTemplate.parent_id == parent.id)
        if expand:
            statement = statement.options(
                selectinload(ResourceTemplate.types),
                selectinload(ResourceTemplate.parent),
                selectinload(ResourceTemplate.children),
                selectinload(ResourceTemplate.attribute_group_templates),
            )
        template = load_single(self.session, statement, label="ResourceTemplate")

        if expand:
            return ResourceTemplateSchema.model_validate(template)
        return ResourceTemplateRef.model_validate(template)

    def create_resource(
        self,
        name: str,
        resource_template: ResourceTemplateRef | ResourceTemplateSchema,
        parent_resource: ResourceRef | ResourceSchema | None = None,
        expand=False,
    ) -> ResourceRef | ResourceSchema:
        parent_id = parent_resource.id if parent_resource else None
        template_model = self.session.get(ResourceTemplate, resource_template.id)
        existing = self.session.scalars(
            select(Resource).where(
                Resource.parent_id == parent_id,
                Resource.name == name,
            )
        ).one_or_none()
        if existing:
            parent_label = getattr(parent_resource, "name", None) or "__root__"
            raise ValidationError(
                "resource",
                f"Resource {name!r} already exists under parent {parent_label!r}",
            )
        resource = Resource(
            name=name,
            resource_template_id=resource_template.id,
            parent_id=parent_id,
            template=template_model,
        )
        self.session.add(resource)
        self.session.flush()
        if expand:
            return ResourceSchema.model_validate(resource)
        return ResourceRef.model_validate(resource)

    def get_resource(
        self,
        name: str,
        template_name: str,
        template_version: str | None = "1.0",
        expand: bool = False,
    ) -> ResourceRef | ResourceSchema:
        stmt = (
            select(Resource)
            .join(Resource.template)
            .where(
                Resource.name == name,
                ResourceTemplate.name == template_name,
                ResourceTemplate.version == template_version,
                Resource.active.is_(True),
            )
        )
        if expand:
            stmt = stmt.options(
                selectinload(Resource.children).selectinload(Resource.children),
                selectinload(Resource.template),
                selectinload(Resource.children).selectinload(Resource.properties),
                selectinload(Resource.properties),
            )
        with self._session_scope() as session:
            resource = load_single(session, stmt, label="Resource")

        if expand:
            return ResourceSchema.model_validate(resource)

        return ResourceRef.model_validate(resource)

    def create_process_run(
        self,
        name: str,
        description: str,
        process_template: ProcessTemplateRef | ProcessTemplateSchema,
        campaign: CampaignSchema,
    ) -> ProcessRunSchema:
        statement = select(ProcessTemplate).where(
            ProcessTemplate.id == process_template.id
        )
        process_template_model = load_single(
            self.session, statement, label="ProcessTemplate"
        )
        process_run = ProcessRun(
            name=name,
            description=description,
            template=process_template_model,
            campaign_id=campaign.id,
        )
        self.session.add(process_run)
        self.session.flush()
        return ProcessRunSchema.model_validate(process_run)

    def assign_resource(
        self,
        resource_slot: ResourceSlotSchema,
        resource: ResourceRef | ResourceSchema,
        process_run: ProcessRunSchema,
    ) -> ProcessRunSchema:
        resource_slot_model = load_single(
            self.session,
            select(ResourceSlot).where(ResourceSlot.id == resource_slot.id),
            label="ResourceSlot",
        )
        resource_model = load_single(
            self.session,
            select(Resource)
            .where(Resource.name == resource.name)
            .options(
                selectinload(Resource.template).selectinload(ResourceTemplate.types)
            ),
            label="Resource",
        )
        process_run_model = load_single(
            self.session,
            select(ProcessRun)
            .where(ProcessRun.id == process_run.id)
            .options(
                selectinload(ProcessRun.assignments),
                selectinload(ProcessRun.template),
            ),
            label="ProcessRun",
        )

        if (
            resource_slot_model.process_template_id
            != process_run_model.process_template_id
        ):
            raise ValueError(
                f"Resource slot {resource_slot_model.name!r} does not belong to "
                f"process template for run {process_run_model.name!r}"
            )

        if resource_model.template is None:
            raise ValueError(
                f"Resource {resource_model.name!r} must have a template with types"
            )

        if not resource_model.active:
            raise ValueError(f"Resource {resource_model.name!r} is inactive")

        template_type_ids = {rt.id for rt in resource_model.template.types}
        if resource_slot_model.resource_type_id not in template_type_ids:
            raise ValueError(
                f"Resource {resource_model.name!r} does not match required type for "
                f"slot {resource_slot_model.name!r}"
            )

        for existing in process_run_model.assignments.values():
            if existing.resource_slot_id == resource_slot_model.id:
                raise ValueError(
                    f"Slot {resource_slot_model.name!r} is already assigned in "
                    f"run {process_run_model.name!r}"
                )

        try:
            process_run_model.resources[resource_slot_model] = resource_model
        except ValueError as exc:
            raise ValueError(
                f"Could not assign resource {resource_model.name!r} "
                f"to slot {resource_slot_model.name!r}: {exc}"
            ) from exc

        return ProcessRunSchema.model_validate(process_run_model)

    def check_resource_assignment(
        self,
        process_template: ProcessTemplateRef | ProcessTemplateSchema,
        process_run: ProcessRunSchema,
    ):
        statement = select(ResourceSlot).where(
            ResourceSlot.process_template_id == process_template.id,
        )
        _resource_slots = self.session.scalars(statement).all()
        expected_ids = {slot.id for slot in _resource_slots}
        assigned_ids = {ar.slot.id for ar in process_run.assigned_resources}

        missing_ids = expected_ids - assigned_ids
        if not missing_ids:
            return

        missing_names = [
            slot.name for slot in _resource_slots if slot.id in missing_ids
        ]
        raise ValueError(
            f"Process run {process_run.name} is missing resources for slots: "
            f"{', '.join(missing_names)}"
        )

    def get_steps(self, process_run: ProcessRunSchema) -> list[StepSchema]:
        statement = (
            select(Step)
            .where(Step.process_run_id == process_run.id)
            .options(
                selectinload(Step.children),
                selectinload(Step.parameters).selectinload(Parameter._values),
                selectinload(Step.assignments)
                .selectinload(ResourceAssignment.resource)
                .selectinload(Resource.template),
                selectinload(Step.assignments).selectinload(
                    ResourceAssignment.resource_slot
                ),
            )
        )
        steps = self.session.scalars(statement).all()
        return [StepSchema.model_validate(step) for step in steps]

    def get_params(self, step_schema: StepSchema) -> type[BaseModel]:
        statement = select(Step).where(
            Step.id == step_schema.id,
        )
        step: Step | None = self.session.scalars(statement).one_or_none()
        if step is None:
            raise LookupError(f"Step not found: {step_schema.name}")
        params: dict[str, tuple] = {
            "step_name": (
                Literal[f"{step_schema.name}"],
                Field(default=step_schema.name),
            ),
            "step_id": (UUID, Field(default=step.id)),
        }
        for _name, param in step.parameters.items():
            tmpl_key = tuple(
                (
                    vt.name,
                    vt.slug,
                    vt.value_type,
                    vt.metadata_json,
                )
                for vt in param.template.attribute_templates
            )
            values_model = build_param_values_model(param.template.slug, tmpl_key)
            params[param.template.slug] = (
                values_model,
                Field(
                    default_factory=lambda vm=values_model,
                    values=param.values: vm.model_validate(values),
                    alias=param.template.name,
                ),
            )
        model = create_model(
            f"{step_schema.name}", **params, __base__=(AliasMixin, BaseModel)
        )
        return model()

    def set_params(self, filled_params: type[BaseModel]):
        statement = select(Step).where(Step.id == filled_params.step_id)
        step: Step | None = self.session.scalars(statement).one_or_none()
        if step is None:
            raise LookupError(f"Step not found in database: {filled_params.step_name}")
        for param in step.parameters.values():
            filled_param = filled_params.get(param.template.name)
            for value_name in step.parameters[param.template.name].values:
                step.parameters[param.template.name].values[value_name] = (
                    filled_param.get(value_name)
                )

    def add_child_step(
        self, process_run: ProcessRunSchema, child_step: StepSchema
    ) -> StepSchema:
        pr_model = load_single(
            self.session,
            select(ProcessRun)
            .where(ProcessRun.id == process_run.id)
            .options(
                selectinload(ProcessRun.steps)
                .selectinload(Step.parameters)
                .selectinload(Parameter._values),
                selectinload(ProcessRun.steps)
                .selectinload(Step.assignments)
                .selectinload(ResourceAssignment.resource)
                .selectinload(Resource.template),
                selectinload(ProcessRun.steps)
                .selectinload(Step.assignments)
                .selectinload(ResourceAssignment.resource_slot),
                selectinload(ProcessRun.assignments)
                .selectinload(ResourceAssignment.resource)
                .selectinload(Resource.children),
            ),
            label="ProcessRun",
        )

        parent_step = next(
            (s for s in pr_model.steps.values() if s.id == child_step.parent_id), None
        )
        if parent_step is None:
            raise ValueError(
                f"Parent step with id {child_step.parent_id} not found in run {pr_model.name}"
            )

        template = load_single(
            self.session,
            select(StepTemplate)
            .where(
                StepTemplate.process_template_id == pr_model.process_template_id,
                StepTemplate.id == child_step.template.id,
            )
            .options(
                selectinload(StepTemplate.bindings).selectinload(
                    StepTemplateResourceSlotBinding.resource_slot
                )
            ),
            label="StepTemplate",
        )

        step_name = child_step.name if child_step.name else template.name
        if pr_model.steps:
            idx = 1
            while step_name in pr_model.steps:
                idx += 1
                step_name = f"{step_name} ({idx})"

        step = Step(template=template, parent=parent_step, name=step_name)
        pr_model.steps[step.name] = step
        # Add early to session to avoid autoflush warnings when binding children/resources
        self.session.add(step)

        if child_step.parameters:
            for group_name, params in child_step.parameters.items():
                if group_name not in step.parameters:
                    raise ValueError(
                        f"Step {step_name} has no parameter group {group_name}"
                    )
                param = step.parameters[group_name]
                for key, value in params.values.items():
                    if key not in param.values:
                        raise ValueError(
                            f"Parameter {key} not found in group {group_name}"
                        )
                    param.values[key] = value

        if child_step.resources:
            self._assign_step_resources(step, pr_model, child_step.resources)

        self.session.add(step)
        self.session.flush()
        return StepSchema.model_validate(step)

    def _assign_step_resources(
        self,
        step: Step,
        process_run_model: ProcessRun,
        resources: dict[str, ResourceRef | ResourceSchema],
    ):
        slot_by_role = {
            b.role: b.resource_slot for b in step.template.bindings.values()
        }
        for role, resource_ref in resources.items():
            if role not in slot_by_role:
                raise ValueError(
                    f"Role {role} is not bound to a resource slot for step {step.template.name}"
                )
            resource_model = self._load_resource_model(resource_ref)
            slot = slot_by_role[role]
            root_resource = process_run_model.resources.get(slot)
            if root_resource is None:
                raise ValueError(
                    f"No resource assigned to slot {slot.name} for process run {process_run_model.name}"
                )
            if not self._resource_is_descendant_or_same(resource_model, root_resource):
                raise ValueError(
                    f"Resource {resource_model.name} is not allowed for role {role}; "
                    f"must be the assigned resource {root_resource.name} or its child"
                )
            assignment = ResourceAssignment(
                process_run=process_run_model,
                resource_slot=slot,
                resource_slot_id=slot.id,  # ensure collection key is populated
                resource=resource_model,
                step=step,
            )
            # Explicitly add to the session before attaching to the mapped collection
            # to avoid KeyFuncDict errors when SQLAlchemy derives the dict key.
            self.session.add(assignment)
            step.assignments[slot.id] = assignment

    def _resource_is_descendant_or_same(self, candidate: Resource, root: Resource):
        current = candidate
        while current is not None:
            if current.id == root.id:
                return True
            current = current.parent
        return False

    def _load_resource_model(self, ref: ResourceRef | ResourceSchema) -> Resource:
        return load_single(
            self.session,
            select(Resource).where(Resource.id == ref.id),
            label="Resource",
        )

    def _coerce_value(self, pf, left_expr):
        type_mapping = {
            "int": Integer,
            "float": Float,
            "bool": Boolean,
            "datetime": String,
        }
        sa_type = type_mapping.get(pf.value_type)
        if sa_type:
            left_expr = cast(left_expr, sa_type)

        coerced_value = pf.value
        if pf.value_type:
            coerced_value = to_json_compatible(pf.value_type, pf.value)
        return coerced_value

    def _between_filter(self, prop_filter, conditions, left_expr):
        if prop_filter.upper is None:
            raise ValueError("between comparator requires upper bound")
        conditions.append(
            left_expr.between(
                to_json_compatible(prop_filter.value_type, prop_filter.value),
                to_json_compatible(prop_filter.value_type, prop_filter.upper),
            )
        )

    def _in_filter(self, prop_filter, conditions, left_expr):
        if prop_filter.value is None:
            raise ValueError("in comparator requires an iterable of values")
        values = prop_filter.value
        if not isinstance(values, list | tuple | set):
            raise ValueError("in comparator requires an iterable of values")
        conditions.append(
            left_expr.in_(
                [to_json_compatible(prop_filter.value_type, v) for v in values]
            )
        )

    def _add_filters(self, pf, conditions, left_expr, coerced_value):
        match pf.op:
            case "eq":
                conditions.append(left_expr == coerced_value)
            case "gt":
                conditions.append(left_expr > coerced_value)
            case "gte":
                conditions.append(left_expr >= coerced_value)
            case "lt":
                conditions.append(left_expr < coerced_value)
            case "lte":
                conditions.append(left_expr <= coerced_value)
            case "between":
                self._between_filter(pf, conditions, left_expr)
            case "in":
                self._in_filter(pf, conditions, left_expr)
            case _:
                raise ValueError(f"Unsupported property comparator {pf.op}")

        return conditions

    def _build_conditions_attributes(
        self,
        pf,
        tmpl_alias: type[AttributeTemplate],
        val_alias: type[AttributeValue],
        group_alias: type[AttributeGroupTemplate] | None,
    ) -> list:
        conditions = []
        name_slug = make_slug(pf.name)
        conditions.append(or_(tmpl_alias.name == pf.name, tmpl_alias.slug == name_slug))

        if group_alias is not None and pf.group:
            group_slug = make_slug(pf.group)
            conditions.append(
                or_(group_alias.name == pf.group, group_alias.slug == group_slug)
            )

        if pf.value_type:
            conditions.append(tmpl_alias.value_type == pf.value_type)

        left_expr = val_alias.value_json
        coerced_value = self._coerce_value(pf, left_expr)
        conditions = self._add_filters(pf, conditions, left_expr, coerced_value)

        return conditions

    def _build_select_resource(self, model, stmt, spec):
        if spec.parent_resource_id:
            res_tbl = Resource.__table__
            root_id = spec.parent_resource_id
            base_cte = (
                select(res_tbl.c.id).where(res_tbl.c.id == root_id).cte(recursive=True)
            )
            children = select(res_tbl.c.id).where(res_tbl.c.parent_id == base_cte.c.id)
            descendants_cte = base_cte.union_all(children)
            stmt = stmt.join(descendants_cte, model.id == descendants_cte.c.id)
            stmt = stmt.where(model.id != root_id)

        if spec.property_filters:
            for idx, pf in enumerate(spec.property_filters):
                prop_alias = aliased(Property, name=f"prop_filter_{idx}")
                val_alias = aliased(AttributeValue, name=f"prop_val_filter_{idx}")
                tmpl_alias = aliased(AttributeTemplate, name=f"prop_tmpl_filter_{idx}")
                group_alias = (
                    aliased(AttributeGroupTemplate, name=f"prop_group_filter_{idx}")
                    if pf.group
                    else None
                )

                stmt = stmt.join(prop_alias, prop_alias.resource_id == model.id)
                stmt = stmt.join(val_alias, val_alias.property_id == prop_alias.id)
                stmt = stmt.join(
                    tmpl_alias, tmpl_alias.id == val_alias.attribute_template_id
                )
                if group_alias is not None:
                    stmt = stmt.join(
                        group_alias,
                        group_alias.id == prop_alias.attribute_group_template_id,
                    )
                conditions = self._build_conditions_attributes(
                    pf, tmpl_alias, val_alias, group_alias
                )
                stmt = stmt.where(and_(*conditions))

            stmt = stmt.distinct()
        return stmt

    def _build_select_process_run(self, model, stmt, spec):
        if spec.parameter_filters:
            for idx, pf in enumerate(spec.parameter_filters):
                step_alias = aliased(Step, name=f"param_step_filter_{idx}")
                param_alias = aliased(Parameter, name=f"param_filter_{idx}")
                val_alias = aliased(AttributeValue, name=f"param_val_filter_{idx}")
                tmpl_alias = aliased(AttributeTemplate, name=f"param_tmpl_filter_{idx}")
                group_alias = (
                    aliased(AttributeGroupTemplate, name=f"param_group_filter_{idx}")
                    if pf.group
                    else None
                )

                stmt = stmt.join(step_alias, step_alias.process_run_id == model.id)
                stmt = stmt.join(param_alias, param_alias.step_id == step_alias.id)
                stmt = stmt.join(val_alias, val_alias.parameter_id == param_alias.id)
                stmt = stmt.join(
                    tmpl_alias, tmpl_alias.id == val_alias.attribute_template_id
                )
                if group_alias is not None:
                    stmt = stmt.join(
                        group_alias,
                        group_alias.id == param_alias.attribute_group_template_id,
                    )

                conditions = self._build_conditions_attributes(
                    pf, tmpl_alias, val_alias, group_alias
                )
                stmt = stmt.where(and_(*conditions))
            stmt = stmt.distinct()
        return stmt

    def _apply_campaign_scope(self, model, stmt, spec):
        if spec.campaign_id is None:
            return stmt

        campaign_id = spec.campaign_id
        if model is ProcessRun:
            return stmt.where(ProcessRun.campaign_id == campaign_id)
        if model is Resource:
            assignment_alias = aliased(
                ResourceAssignment, name="campaign_resource_assignment"
            )
            pr_alias = aliased(ProcessRun, name="campaign_process_run")
            stmt = stmt.join(assignment_alias, assignment_alias.resource_id == model.id)
            stmt = stmt.join(pr_alias, pr_alias.id == assignment_alias.process_run_id)
            stmt = stmt.where(pr_alias.campaign_id == campaign_id)
            return stmt.distinct()
        return stmt

    def _build_select(self, schema: type[SchemaT], spec: QuerySpec) -> Select:
        model = SCHEMA_MODEL_MAPPING[schema]
        stmt = select(model)

        if model is ResourceTemplate and "types__names_in" in spec.filters:
            type_names = spec.filters.pop("types__names_in")
            stmt = (
                stmt.join(ResourceTemplate.types)
                .where(ResourceType.name.in_(type_names))
                .group_by(ResourceTemplate.id)
            )

        filters = dict(spec.filters)
        joined_paths: dict[tuple[str, ...], type] = {}
        simple_filters: dict[str, object] = {}

        for raw_key, value in filters.items():
            if "__" not in raw_key:
                simple_filters[raw_key] = value
                continue
            parts = tuple(raw_key.split("__"))
            stmt, attr = resolve_path(model, stmt, parts, joined_paths)
            stmt = stmt.where(attr == value)

        if simple_filters:
            stmt = stmt.filter_by(**simple_filters)

        if model is Resource:
            stmt = self._build_select_resource(model, stmt, spec)

        if model is ProcessRun:
            stmt = self._build_select_process_run(model, stmt, spec)

        stmt = self._apply_campaign_scope(model, stmt, spec)

        for pred in spec.predicates:
            stmt = stmt.where(pred)

        if spec.orderings:
            stmt = stmt.order_by(*spec.orderings)

        return stmt

    def query(self, schema: type[SchemaT], spec: QuerySpec) -> list[SchemaT]:
        stmt = self._build_select(schema, spec)

        loader_options = self._relationship_loaders(schema, list(spec.preloads))
        if loader_options:
            stmt = stmt.options(*loader_options)

        if spec.limit is not None:
            stmt = stmt.limit(spec.limit)
        if spec.offset is not None:
            stmt = stmt.offset(spec.offset)

        with self._session_scope() as session:
            return [
                schema.model_validate(obj)
                for obj in list(session.scalars(stmt).unique())
            ]

    def count(self, schema: type[SchemaT], spec: QuerySpec) -> int:
        # model = SCHEMA_MODEL_MAPPING[schema]
        stmt = self._build_select(schema, spec)

        with self._session_scope() as session:
            select_stmt = select(count()).select_from(stmt.subquery())
            return session.execute(select_stmt).scalar_one()

    def _relationship_loaders(self, schema: type[SchemaT], preloads: list[str]):
        model = SCHEMA_MODEL_MAPPING[schema]
        opts = []
        if model is Resource and schema is ResourceRef:
            opts.append(
                selectinload(Resource.template).selectinload(ResourceTemplate.types)
            )
        if model is ResourceTemplate and schema in {
            ResourceTemplateRef,
            ResourceTemplateSchema,
        }:
            opts.append(selectinload(ResourceTemplate.types))
        if model is ProcessRun and schema in {ProcessRunRef, ProcessRunSchema}:
            opts.append(selectinload(ProcessRun.template))
        for name in preloads:
            opts.append(self.get_opts_statements(schema, name))
        return opts

    def get_opts_statements(self, schema, name):
        statements = {
            (ProcessRunSchema, "steps"): selectinload(ProcessRun.steps)
            .selectinload(Step.children)
            .selectinload(Step.parameters),
            (ProcessRunSchema, "steps.parameters"): selectinload(ProcessRun.steps)
            .selectinload(Step.parameters)
            .selectinload(Parameter._values),
            (ProcessRunSchema, "resources"): selectinload(
                ProcessRun.assignments
            ).selectinload(ResourceAssignment.resource),
            (CampaignSchema, "process_run"): selectinload(Campaign.process_runs),
            (ProcessTemplateSchema, "step_templates"): selectinload(
                ProcessTemplate.step_templates
            ),
            (ProcessTemplateSchema, "resource_slots"): selectinload(
                ProcessTemplate.resource_slots
            ),
            (ResourceTemplateSchema, "children"): selectinload(
                ResourceTemplate.children
            ),
            (ResourceTemplateSchema, "attribute_group_templates"): selectinload(
                ResourceTemplate.attribute_group_templates
            ),
            (ResourceTemplateSchema, "types"): selectinload(ResourceTemplate.types),
            (ResourceSchema, "properties"): selectinload(
                Resource.properties
            ).selectinload(Property._values),
            (ResourceRef, "properties"): selectinload(Resource.properties).selectinload(
                Property._values
            ),
            (ResourceSchema, "children"): selectinload(Resource.children),
            (ResourceRef, "children"): selectinload(Resource.children),
            (ResourceSchema, "template"): selectinload(Resource.template),
            (ResourceRef, "template"): selectinload(Resource.template),
        }
        return statements[(schema, name)]

    def update_process_run(self, process_run: ProcessRunSchema) -> ProcessRunSchema:
        with self._session_scope() as session:
            owns_tx = not session.in_transaction()
            tx = session.begin() if owns_tx else None
            for step_schema in process_run.steps.values():
                step: Step = load_single(
                    session,
                    select(Step)
                    .where(Step.id == step_schema.id)
                    .options(
                        selectinload(Step.parameters).selectinload(Parameter._values)
                    ),
                    label="Step",
                )
                for _, param_schema in step_schema.parameters.items():
                    group_name = param_schema.template.name
                    if group_name not in step.parameters:
                        raise ValueError(
                            f"Step {step_schema.name} has no parameter group {group_name}"
                        )
                    param = step.parameters[group_name]

                    new_values = param_schema.values.model_dump(by_alias=True)
                    for key, value in new_values.items():
                        if key not in param.values:
                            raise ValueError(
                                f"Parameter {key} not found in group {group_name}"
                            )
                        param.values[key] = value
            session.flush()
            if tx:
                tx.commit()
        return process_run

    def update_resource(self, resource: ResourceSchema) -> ResourceSchema:
        with self._session_scope() as session:
            owns_tx = not session.in_transaction()
            tx = session.begin() if owns_tx else None
            res: Resource = load_single(
                session,
                select(Resource)
                .where(Resource.id == resource.id)
                .options(
                    selectinload(Resource.properties).selectinload(Property._values),
                    selectinload(Resource.children),
                ),
                label="Resource",
            )
            for _, prop_schema in resource.properties.items():
                tmpl_name = prop_schema.template.name
                if tmpl_name not in res.properties:
                    raise ValueError(
                        f"Resource {resource.name} has no property group {tmpl_name}"
                    )
                prop = res.properties[tmpl_name]
                new_values = prop_schema.values.model_dump(by_alias=True)
                for key, value in new_values.items():
                    if key not in prop.values:
                        raise ValueError(
                            f"Property {key} not found in group {tmpl_name}"
                        )
                    prop.values[key] = value
            session.flush()
            if tx:
                tx.commit()
            return ResourceSchema.model_validate(res)
