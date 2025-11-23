import warnings
from contextlib import contextmanager
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, create_model
from sqlalchemy import insert, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session, selectinload

from recap.adapter import Backend, UnitOfWork
from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.db.base import Base
from recap.db.campaign import Campaign
from recap.db.process import (
    Direction,
    ProcessRun,
    ProcessTemplate,
    ResourceAssignment,
    ResourceSlot,
)
from recap.db.resource import (
    Resource,
    ResourceTemplate,
    ResourceType,
    resource_template_type_association,
)
from recap.db.step import Parameter, Step, StepTemplate
from recap.dsl.process_builder import map_dtype_to_pytype
from recap.dsl.query import QuerySpec, SchemaT
from recap.schemas.attribute import AttributeGroupRef, AttributeTemplateSchema
from recap.schemas.process import (
    CampaignSchema,
    ProcessRunSchema,
    ProcessTemplateRef,
    ProcessTemplateSchema,
    ResourceRef,
    ResourceSchema,
    ResourceTemplateRef,
    ResourceTemplateSchema,
)
from recap.schemas.resource import ResourceSlotSchema, ResourceTypeSchema
from recap.schemas.step import StepSchema, StepTemplateRef
from recap.utils.database import _load_single
from recap.utils.dsl import AliasMixin, _get_or_create

SCHEMA_MODEL_MAPPING: dict[type[BaseModel], type[Base]] = {
    CampaignSchema: Campaign,
    ResourceTemplateSchema: ResourceTemplate,
    ProcessRunSchema: ProcessRun,
    ResourceSchema: Resource,
}


class SQLUnitOfWork(UnitOfWork):
    def __init__(self, backend: "LocalBackend", session: Session, tx):
        self._backend = backend
        self._session = session
        self._tx = tx

    def commit(self):
        self._tx.commit()
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
        template, created = _get_or_create(
            self.session, ProcessTemplate, {"name": name, "version": version}, {}
        )
        if created:
            return ProcessTemplateRef.model_validate(template)

    def get_process_template(
        self, name: str, version: str | None, expand: bool = False
    ) -> ProcessTemplateRef | ProcessTemplateSchema:
        statement = select(ProcessTemplate).filter_by(name=name, version=version)
        if expand:
            statement.options(
                selectinload(ProcessTemplate.step_templates),
                selectinload(ProcessTemplate.resource_slots),
            )
        process_template = self.session.execute(statement).scalar_one_or_none()
        if process_template is None:
            raise ValueError(
                f"Could not find process template by name: {name} version: {version}"
            )
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
        slot, _ = _get_or_create(
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
        if isinstance(template_ref, StepTemplateRef):
            filter_params["step_template_id"] = template_ref.id
        elif isinstance(template_ref, ResourceTemplateRef):
            filter_params["resource_template_id"] = template_ref.id
        attr_group_template: AttributeGroupTemplate | None = self.session.execute(
            select(AttributeGroupTemplate).filter_by(**filter_params)
        ).scalar_one_or_none()
        if attr_group_template is None:
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
    ) -> AttributeTemplateSchema:
        filter_params: dict[str, Any] = {
            "name": name,
            "value_type": value_type,
            "unit": unit,
            "default_value": default,
            "attribute_group_template_id": attribute_group_ref.id,
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
            resource_type, _ = _get_or_create(self.session, ResourceType, where=where)
            resource_type_schemas.append(
                ResourceTypeSchema.model_validate(resource_type)
            )

        return resource_type_schemas

    def add_resource_template(
        self, name: str, types: list[ResourceTypeSchema]
    ) -> ResourceTemplateRef:
        template = ResourceTemplate(
            name=name,
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
    ) -> ResourceTemplateRef:
        stmt = select(ResourceType).where(
            ResourceType.id.in_([type.id for type in resource_types])
        )
        resource_type_results = self.session.scalars(stmt).all()

        template = ResourceTemplate(
            name=name,
            types=resource_type_results,
        )
        parent_template = self.session.get(
            ResourceTemplate, parent_resource_template.id
        )
        if parent_template is None:
            raise NoResultFound(
                f"Parent template: {parent_resource_template.name} with id {parent_resource_template.id} not found"
            )
        parent_template.children.append(template)
        self.session.add(template)
        self.session.flush()
        return ResourceTemplateRef.model_validate(template)

    def get_resource_template(
        self,
        name: str,
        id: UUID | str | None = None,
        parent: ResourceTemplateRef | ResourceTemplate | None = None,
        expand: bool = False,
    ) -> ResourceTemplateRef | ResourceTemplateSchema:
        statement = select(ResourceTemplate).where(ResourceTemplate.name == name)
        if isinstance(id, str):
            id = UUID(id)
        if id:
            statement = statement.where(ResourceTemplate.id == UUID(id))
        if parent:
            statement = statement.where(ResourceTemplate.parent_id == parent.id)
        if expand:
            statement = statement.options(
                selectinload(ResourceTemplate.types),
                selectinload(ResourceTemplate.parent),
                selectinload(ResourceTemplate.children),
                selectinload(ResourceTemplate.attribute_group_templates),
            )
        template = _load_single(self.session, statement, label="ResourceTemplate")

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
        resource = Resource(
            name=name, resource_template_id=resource_template.id, parent_id=parent_id
        )
        self.session.add(resource)
        self.session.flush()
        if expand:
            return ResourceSchema.model_validate(resource)
        return ResourceRef.model_validate(resource)

    def get_resource(self, name: str, template_name: str) -> ResourceRef:
        statement = (
            select(Resource)
            .join(Resource.template)
            .where(
                Resource.name == name,
                ResourceTemplate.name == template_name,
                Resource.active.is_(True),
            )
        )
        resource = _load_single(self.session, statement, label="Resource")
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
        process_template_model = _load_single(
            self.session, statement, label="ProcessTemplate"
        )
        process_run = ProcessRun(
            name=name,
            description=description,
            template=process_template_model,
            campaign_id=campaign.id,
        )
        print(process_run.name)
        self.session.add(process_run)
        self.session.flush()

        return ProcessRunSchema.model_validate(process_run)

    def assign_resource(
        self,
        resource_slot: ResourceSlotSchema,
        resource: ResourceRef | ResourceSchema,
        process_run: ProcessRunSchema,
    ):
        statement = select(ResourceSlot).where(
            ResourceSlot.process_template_id == process_run.template.id
        )
        if resource_slot.name:
            statement = statement.where(
                ResourceSlot.name == resource_slot.name,
            )
        if resource.id:
            statement = statement.where(ResourceSlot.id == resource.id)
        resource_statement = select(Resource).where(
            Resource.name == resource.name, Resource.active
        )
        _resource_slot = _load_single(self.session, statement, label="ResourceSlot")
        _resource = _load_single(self.session, resource_statement, label="Resource")

        # self._process_run.resources[_resource_slot] = _resource

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
        assigned_ids = {slot.id for slot in process_run.assignments}

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
        statement = select(Step).where(Step.process_run_id == process_run.id)
        steps = self.session.scalars(statement).all()
        return [StepSchema.model_validate(step) for step in steps]

    def get_params(self, step_schema: StepSchema) -> type[BaseModel]:
        statement = select(Step).where(
            Step.process_run_id == step_schema.process_run_id,
            Step.name == step_schema.id,
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
            param_fields: dict[str, tuple] = {}
            for val_name, value in param.values.items():
                value_template = None
                for vt in param.template.attribute_templates:
                    if vt.name == val_name:
                        value_template = vt
                        break
                if value_template is None:
                    raise LookupError(f"Could not find value with name {val_name}")
                pytype = map_dtype_to_pytype(value_template.value_type)
                param_fields[value_template.slug] = (
                    pytype | None,
                    Field(default=value, alias=value_template.name),
                )
                param_model = create_model(
                    f"{val_name}", **param_fields, __base__=(AliasMixin, BaseModel)
                )
                params[param.template.slug] = (
                    param_model,
                    Field(default_factory=param_model, alias=param.template.name),
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

    def query(self, schema: type[SchemaT], spec: QuerySpec) -> list[SchemaT]:
        model = SCHEMA_MODEL_MAPPING[schema]
        stmt = select(model)

        if model is ResourceTemplate and "types__names_in" in spec.filters:
            type_names = spec.filters.pop("types__names_in")
            stmt = (
                stmt.join(ResourceTemplate.types)
                .where(ResourceType.name.in_(type_names))
                .group_by(ResourceTemplate.id)
            )

        elif spec.filters:
            print(model, spec.filters)
            stmt = stmt.filter_by(**spec.filters)

        for pred in spec.predicates:
            stmt = stmt.where(pred)

        if spec.orderings:
            stmt = stmt.order_by(*spec.orderings)

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
        model = SCHEMA_MODEL_MAPPING[schema]
        stmt = select(model.id)

        if spec.filters:
            stmt = stmt.filter_by(**spec.filters)
        for pred in spec.predicates:
            stmt = stmt.where(pred)

        with self._session_scope() as session:
            return session.execute(stmt.count()).scalar_one()

    def _relationship_loaders(self, schema: type[SchemaT], preloads: list[str]):
        model = SCHEMA_MODEL_MAPPING[schema]
        opts = []
        for name in preloads:
            if model is ProcessRunSchema and name == "steps":
                opts.append(selectinload(ProcessRun.steps))
            elif model is ProcessRunSchema and name == "steps.parameters":
                opts.append(
                    selectinload(ProcessRun.steps)
                    .selectinload(Step.parameters)
                    .selectinload(Parameter._values)
                )
            elif model is ProcessRunSchema and name == "resources":
                opts.append(
                    selectinload(ProcessRun.assignments).selectinload(
                        ResourceAssignment.resource
                    )
                )
            elif model is CampaignSchema and name == "process_run":
                opts.append(selectinload(Campaign.process_runs))
        return opts
