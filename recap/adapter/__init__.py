from typing import Any, Literal, Protocol, overload
from uuid import UUID

from pydantic import BaseModel

from recap.db.process import Direction
from recap.dsl.query import QuerySpec, SchemaT
from recap.schemas.attribute import AttributeGroupRef, AttributeTemplateSchema
from recap.schemas.process import (
    CampaignSchema,
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
from recap.schemas.step import StepSchema, StepTemplateRef


class UnitOfWork(Protocol):
    def commit(self, clear_session: bool = True) -> None: ...
    def rollback(self) -> None: ...
    def end_session(self) -> None: ...


class Backend(Protocol):
    def begin(self) -> UnitOfWork: ...

    ## Create campaign
    def create_campaign(
        self,
        name: str,
        proposal: str,
        saf: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> CampaignSchema: ...

    def set_campaign(self, id: UUID) -> CampaignSchema: ...

    # Process Template creation

    def create_process_template(
        self, name: str, version: str
    ) -> ProcessTemplateRef: ...

    @overload
    def get_process_template(
        self, name: str, version: str | None, expand: Literal[False]
    ) -> ProcessTemplateRef: ...

    @overload
    def get_process_template(
        self, name: str, version: str | None, expand: Literal[True]
    ) -> ProcessTemplateSchema: ...

    def add_resource_slot(
        self,
        name: str,
        resource_type: str,
        direction: Direction,
        process_template_ref: ProcessTemplateRef,
        create_resource_type=False,
    ) -> ResourceSlotSchema: ...

    def add_step(
        self, name: str, process_template_ref: ProcessTemplateRef
    ) -> StepTemplateRef: ...

    def bind_slot(
        self,
        role: str,
        slot_name: str,
        process_template_ref: ProcessTemplateRef,
        step_template_ref: StepTemplateRef,
    ) -> ResourceSlotSchema: ...

    def add_attr_group(
        self, group_name: str, template: ResourceTemplateRef | StepTemplateRef
    ) -> AttributeGroupRef: ...

    def add_attribute(
        self,
        name: str,
        value_type: str,
        unit: str,
        default: Any,
        attribute_group_ref: AttributeGroupRef,
    ) -> AttributeTemplateSchema: ...

    def remove_attribute(self, name: str): ...

    # def close_step(self, step_template_ref: StepTemplateRef) -> StepTemplateSchema: ...

    # Resource template creation

    def add_resource_types(self, type_names: list[str]) -> list[ResourceTypeSchema]: ...

    def add_resource_template(
        self, name: str, type_names: list[ResourceTypeSchema], version: str = "1.0"
    ) -> ResourceTemplateRef: ...

    def add_child_resource_template(
        self,
        name: str,
        resource_types: list[ResourceTypeSchema],
        parent_resource_template: ResourceTemplateRef | ResourceTemplateSchema,
        version: str = "1.0",
    ) -> ResourceTemplateRef: ...

    @overload
    def get_resource_template(
        self,
        name: str | None,
        version: str | None = None,
        id: UUID | str | None = None,
        parent: ResourceTemplateRef | ResourceTemplateSchema | None = None,
        expand: Literal[False] = False,
    ) -> ResourceTemplateRef: ...

    @overload
    def get_resource_template(
        self,
        name: str | None,
        version: str | None = None,
        id: UUID | str | None = None,
        parent: ResourceTemplateRef | ResourceTemplateSchema | None = None,
        expand: Literal[True] = False,
    ) -> ResourceTemplateSchema: ...

    # Resource creation

    @overload
    def create_resource(
        self,
        name: str,
        resource_template: ResourceTemplateRef | ResourceTemplateSchema,
        parent_resource: ResourceRef | ResourceSchema | None,
        expand: Literal[False],
    ) -> ResourceRef: ...

    @overload
    def create_resource(
        self,
        name: str,
        resource_template: ResourceTemplateRef | ResourceTemplateSchema,
        parent_resource: ResourceRef | ResourceSchema | None,
        expand: Literal[True],
    ) -> ResourceSchema: ...

    def get_resource(
        self,
        name: str,
        template_name: str,
        template_version: str | None = "1.0",
        expand: bool = False,
    ) -> ResourceSchema: ...

    def add_child_resources(
        self,
        parent_resource: ResourceSchema | ResourceRef,
        child_resources: list[ResourceSchema | ResourceRef],
    ): ...

    def create_process_run(
        self,
        name: str,
        description: str,
        process_template: ProcessTemplateRef | ProcessTemplateSchema,
        campaign: CampaignSchema,
    ) -> ProcessRunSchema: ...

    def assign_resource(
        self,
        resource_slot: ResourceSlotSchema,
        resource: ResourceRef | ResourceSchema,
        process_run: ProcessRunSchema,
    ) -> ProcessRunSchema: ...
    def update_resource(self, resource: ResourceSchema) -> ResourceSchema: ...

    def check_resource_assignment(
        self,
        process_template: ProcessTemplateRef | ProcessTemplateSchema,
        process_run: ProcessRunSchema,
    ): ...

    def get_steps(self, process_run: ProcessRunSchema) -> list[StepSchema]: ...

    def get_params(self, step_schema: StepSchema) -> type[BaseModel]: ...

    def set_params(self, filled_params: type[BaseModel]): ...

    def query(self, schema: type[SchemaT], spec: QuerySpec) -> list[SchemaT]: ...

    def count(self, schema: type[SchemaT], spec: QuerySpec) -> int: ...

    def update_process_run(self, process_run: ProcessRunSchema) -> ProcessRunSchema: ...

    def add_child_step(
        self,
        process_run: ProcessRunSchema,
        parent_step_id: UUID,
        step_template_name: str,
        parameter_values: dict[str, dict[str, Any]] | None = None,
        resources: dict[str, ResourceRef | ResourceSchema] | None = None,
        step_name: str | None = None,
    ) -> StepSchema: ...
