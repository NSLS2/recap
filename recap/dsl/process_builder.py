import warnings
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, create_model
from sqlalchemy import select
from sqlalchemy.orm import Session

from recap.models import (
    AttributeTemplate,
    AttributeValueTemplate,
    ProcessTemplate,
)
from recap.models.process import Direction, ProcessRun, ResourceSlot
from recap.models.resource import Resource, ResourceTemplate, ResourceType
from recap.models.step import Step, StepTemplate
from recap.utils.dsl import AliasMixin, _get_or_create


class ProcessTemplateBuilder:
    def __init__(self, session: Session, name: str, version: str | None = None):
        self.session = session
        self._tx = session.begin()
        self.name = name
        self.version = version
        self._template: ProcessTemplate | None = None
        self._resource_slots: dict[str, ResourceSlot] = {}
        self._current_step_builder: StepTemplateBuilder | None = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.save()
        else:
            self._tx.rollback()
        self.close()

    def save(self):
        self.session.flush()
        self._tx.commit()
        return self

    def close(self):
        self.session.close()

    @property
    def template(self) -> ProcessTemplate:
        if not self._template:
            raise RuntimeError(
                "Call .save() first or construct template via builder methods"
            )
        return self._template

    def _ensure_template(self):
        if self._template:
            return
        where = {"name": self.name}
        defaults = {}
        if hasattr(ProcessTemplate, "version"):
            defaults["version"] = self.version
        tpl, _ = _get_or_create(
            self.session, ProcessTemplate, where=where, defaults=defaults
        )
        self._template = tpl

    def add_resource_slot(
        self,
        name: str,
        resource_type: str,
        direction: Direction,
        create_resource_type=False,
    ) -> "ProcessTemplateBuilder":
        self._ensure_template()
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
            where={"process_template_id": self._template.id, "name": name},
            defaults={"resource_type": rt, "direction": direction},
        )
        if slot.resource_type_id != rt.id and slot.direction != direction:
            raise ValueError(
                f"ResourceSlot {name} already exists with different type/direction"
            )
        self._resource_slots[name] = slot
        return self

    def add_step(
        self,
        name: str,
    ):
        self._ensure_template()
        step_template = StepTemplate(name=name, process_template=self.template)
        self.session.add(step_template)
        step_template_builder = StepTemplateBuilder(parent=self, step=step_template)
        return step_template_builder


class StepTemplateBuilder:
    """Scoped editor for a single step"""

    def __init__(self, parent: ProcessTemplateBuilder, step: StepTemplate):
        self.parent = parent
        self.session = parent.session
        self.process_template = parent.template
        self.step = step

    def complete_step(self) -> ProcessTemplateBuilder:
        return self.parent

    def add_param(
        self,
        group_name: str,
        param_name: str,
        value_type: str,
        unit: str,
        default: str | None = None,
        create_group=False,
    ) -> "StepTemplateBuilder":
        param_group = self.session.execute(
            select(AttributeTemplate).filter_by(name=group_name)
        ).scalar_one_or_none()

        if param_group is None:
            if create_group:
                param_group = AttributeTemplate(name=group_name)
                self.session.add(param_group)
                self.session.flush()
            else:
                raise ValueError(
                    f"Parameter group: {group_name} does not exist in database and create_group = False"
                )

        param_value = self.session.execute(
            select(AttributeValueTemplate).filter_by(
                name=param_name, value_type=value_type, attribute_template=param_group
            )
        ).scalar_one_or_none()
        if param_value is not None:
            warnings.warn(
                f"Parameter {param_name} already exists for {group_name}. Nothing changed",
                stacklevel=2,
            )
        else:
            param_value = AttributeValueTemplate(
                name=param_name,
                value_type=value_type,
                attribute_template=param_group,
                default_value=default,
                unit=unit,
            )
            self.session.add(param_value)
            self.session.flush()
        self.step.attribute_templates.append(param_group)
        return self

    def remove_param(self, group_name: str, param_name: str) -> "StepTemplateBuilder":
        param_group = self.session.execute(
            select(AttributeTemplate)
            .filter_by(name=group_name)
            .where(
                AttributeTemplate.step_templates.any(StepTemplate.id == self.step.id)
            )
        ).scalar_one_or_none()

        if param_group is None:
            warnings.warn(
                f"Parameter group: {group_name} does not exist in database",
                stacklevel=2,
            )
            return self

        param_value = self.session.execute(
            select(AttributeValueTemplate).filter_by(
                name=param_name, attribute_template=param_group
            )
        ).scalar_one_or_none()
        if param_value is None:
            warnings.warn(
                f"Parameter {param_name} does not exist in group {group_name}",
                stacklevel=2,
            )
            return self

        param_group.value_templates.remove(param_value)
        return self

    def bind(self, slot_name: str, role: str):
        slot = self.session.scalars(
            select(ResourceSlot).where(
                ResourceSlot.process_template_id == self.parent.template.id,
                ResourceSlot.name == slot_name,
            )
        ).one_or_none()
        if slot is None:
            warnings.warn(
                f"Did not find ResourceSlot named {slot_name}. Nothing added",
                stacklevel=2,
            )
            return self

        self.step.resource_slots[role] = slot
        return self


class ProcessRunBuilder:
    def __init__(
        self,
        session: Session,
        name: str,
        template_name: str,
        version: str | None = None,
    ):
        self.session = session
        self._tx = session.begin()
        self.name = name
        self.template_name = template_name
        self.version = version
        statement = select(ProcessTemplate).filter_by(
            name=template_name, version=version
        )
        self._process_template = self.session.execute(statement).scalar_one_or_none()
        if self._process_template is None:
            raise ValueError(
                f"Could not find process template by name {self.template_name}"
            )
        self._process_run = ProcessRun(
            name=name, template=self._process_template, description="Test"
        )
        self.session.add(self._process_run)
        self.session.flush()
        self._resources = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.save()
        else:
            self._tx.rollback()
        self.close()

    def save(self):
        self.session.flush()
        self._tx.commit()
        return self

    def close(self):
        self.session.close()

    @property
    def process_run(self) -> ProcessRun:
        return self._process_run

    def create_resource(
        self, resource_name: str, resource_template_name: str
    ) -> "ProcessRunBuilder":
        statement = select(ResourceTemplate).filter_by(name=resource_template_name)
        resource_template = self.session.scalars(statement).one_or_none()
        if resource_template is None:
            raise ValueError(f"Resource template not found: {resource_template_name}")
        resource = Resource(name=resource_name, template=resource_template)
        self.session.add(resource)
        self._resources[resource_name] = resource
        return self

    def assign_resource(
        self,
        resource_slot_name: str,
        resource_name: str,
        resource_id: UUID | None = None,
    ) -> "ProcessRunBuilder":
        statement = select(ResourceSlot).where(
            ResourceSlot.process_template_id == self._process_template.id,
            ResourceSlot.name == resource_slot_name,
        )
        _resource_slot = self.session.scalars(statement).one_or_none()
        if resource_name not in self._resources:
            if resource_id is None:
                raise ValueError(
                    f"Resource name not found in builder and no resource id provided: {resource_name}"
                )
            else:
                statement = select(Resource).where(Resource.id == resource_id)
                resource = self.session.scalars(statement).one_or_none()
                if resource is None:
                    raise ValueError(f"Resource not found with id: {resource_id}")
        self._process_run.resources[_resource_slot] = self._resources[resource_name]
        return self

    def get_params(
        self,
        step_name: str,
    ) -> type[BaseModel]:
        statement = select(Step).where(
            Step.process_run_id == self._process_run.id, Step.name == step_name
        )
        step: Step | None = self.session.scalars(statement).one_or_none()
        if step is None:
            raise ValueError(f"Step not found: {step_name}")
        params: dict[str, tuple] = {
            "step_name": (Literal[f"{step_name}"], Field(default=step_name)),
            "step_id": (UUID, Field(default=step.id)),
        }
        for _name, param in step.parameters.items():
            param_fields: dict[str, tuple] = {}
            for val_name, value in param.values.items():
                value_template = None
                for vt in param.template.value_templates:
                    if vt.name == val_name:
                        value_template = vt
                        break
                if value_template is None:
                    raise ValueError(f"Could not find value with name {val_name}")
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
        model = create_model(f"{step_name}", **params, __base__=(AliasMixin, BaseModel))
        return model()

    def set_params(self, filled_params):
        statement = select(Step).where(Step.id == filled_params.step_id)
        step: Step | None = self.session.scalars(statement).one_or_none()
        if step is None:
            raise ValueError(f"Step not found in database: {filled_params.step_name}")
        for param in step.parameters.values():
            filled_param = filled_params.get(param.template.name)
            for value_name in step.parameters[param.template.name].values:
                step.parameters[param.template.name].values[value_name] = (
                    filled_param.get(value_name)
                )


def map_dtype_to_pytype(dtype: str):
    return {
        "float": float,
        "int": int,
        "str": str,
        "bool": bool,
        "datetime": str,
        "array": list,
    }[dtype]


# class ProcessRunWizard:
#     def __init__(self, session: Session):
#         self.session = session

#     def current(self, run_id: str) -> Prompt:
#         try:
#             run = self.session.get(ProcessRun, run_id)
#             step = compute_current_step(run)
#             if not step:
#                 return Prompt(
#                     meta=StepMeta(ordinal=0, name="Finished", slug="__done__"),
#                     model=create_model("Empty", )
#                 )
