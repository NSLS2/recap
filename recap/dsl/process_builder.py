from pydantic import BaseModel
from sqlalchemy.exc import NoResultFound

from recap.adapter import Backend
from recap.db.process import Direction
from recap.dsl.attribute_builder import AttributeGroupBuilder
from recap.schemas.process import CampaignSchema, ProcessRunSchema, ProcessTemplateRef
from recap.schemas.resource import ResourceSlotSchema
from recap.schemas.step import StepSchema, StepTemplateRef


class ProcessTemplateBuilder:
    def __init__(self, backend: Backend, name: str, version: str):
        self.backend = backend
        self._uow = self.backend.begin()
        self.name = name
        self.version = version
        self._template: ProcessTemplateRef | None = None
        self._resource_slots: dict[str, ResourceSlotSchema] = {}
        self._current_step_builder: StepTemplateBuilder | None = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.save()
        else:
            self._uow.rollback()

    def save(self):
        self._uow.commit()
        return self

    @property
    def template(self) -> ProcessTemplateRef:
        if not self._template:
            raise RuntimeError(
                "Call .save() first or construct template via builder methods"
            )
        return self._template

    def _ensure_template(self):
        if self._template:
            return
        self._template = self.backend.create_process_template(self.name, self.version)

    def add_resource_slot(
        self,
        name: str,
        resource_type: str,
        direction: Direction,
        create_resource_type=False,
    ) -> "ProcessTemplateBuilder":
        self._ensure_template()
        self._resource_slots[name] = self.backend.add_resource_slot(
            name, resource_type, direction, self.template, create_resource_type
        )
        return self

    def add_step(
        self,
        name: str,
    ):
        self._ensure_template()
        step_template = self.backend.add_step(name, self.template)
        step_template_builder = StepTemplateBuilder(
            parent=self, step_template=step_template
        )
        return step_template_builder


class StepTemplateBuilder:
    """Scoped editor for a single step"""

    def __init__(self, parent: ProcessTemplateBuilder, step_template: StepTemplateRef):
        self.parent: ProcessTemplateBuilder = parent
        self.backend: Backend = parent.backend
        self.process_template = parent.template
        self._template = step_template
        self._bound_slots = {}

    def close_step(self) -> ProcessTemplateBuilder:
        return self.parent

    def param_group(
        self, group_name: str
    ) -> "AttributeGroupBuilder[StepTemplateBuilder]":
        attr_group_builder: AttributeGroupBuilder[StepTemplateBuilder] = (
            AttributeGroupBuilder(group_name=group_name, parent=self)
        )
        return attr_group_builder

    def bind_slot(self, role: str, slot_name: str):
        slot = self.backend.bind_slot(
            role, slot_name, self.process_template, self._template
        )
        self._bound_slots[slot.name] = slot
        return self


class ProcessRunBuilder:
    def __init__(
        self,
        name: str,
        description: str,
        template_name: str,
        campaign: CampaignSchema,
        backend: Backend,
        version: str | None = None,
    ):
        # self.session = session
        # self._tx = session.begin_nested()
        self.backend = backend
        self._uow = self.backend.begin()
        self.name = name
        self.template_name = template_name
        self.version = version
        self._process_template = self.backend.get_process_template(
            self.template_name, self.version, expand=True
        )
        self._process_run = self.backend.create_process_run(
            name, description, self._process_template, campaign
        )
        self._steps = None
        self._resources = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.save()
        else:
            self._uow.rollback()

    def save(self):
        self._uow.commit()
        return self

    @property
    def process_run(self) -> ProcessRunSchema:
        return self._process_run

    def assign_resource(
        self, resource_slot_name: str, resource_name: str, resource_template_name: str
    ) -> "ProcessRunBuilder":
        resource_slot = None
        for slot in self._process_template.resource_slots:
            if slot.name == resource_slot_name:
                resource_slot = slot
                break
        resource = self.backend.get_resource(resource_name, resource_template_name)
        if resource_slot is None:
            raise NoResultFound(f"Resource slot {resource_slot_name} not found")
        self.backend.assign_resource(resource_slot, resource, self._process_run)
        return self

    def _check_resource_assignment(self):
        self.backend.check_resource_assignment(self._process_template, self.process_run)

    def steps(self) -> list[StepSchema]:
        self._check_resource_assignment()
        if self._steps is None:
            self._steps = self.backend.get_steps(self.process_run)
        return self._steps

    def get_params(
        self,
        step_schema: StepSchema,
    ) -> type[BaseModel]:
        self._check_resource_assignment()
        return self.backend.get_params(step_schema)

    def set_params(self, filled_params: type[BaseModel]):
        self.backend.set_params(filled_params)


def map_dtype_to_pytype(dtype: str):
    return {
        "float": float,
        "int": int,
        "str": str,
        "bool": bool,
        "datetime": str,
        "array": list,
    }[dtype]
