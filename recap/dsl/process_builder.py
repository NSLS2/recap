import warnings
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, create_model
from sqlalchemy.exc import NoResultFound

from recap.client.backend import ClientBackend
from recap.commands.models import (
    CommandContext,
    CreateProcessRun,
    CreateProcessTemplate,
    SetLifecycleStatus,
    UpdateProcessRun,
    UpdateProcessTemplate,
)
from recap.dsl.attribute_builder import AttributeGroupBuilder
from recap.dsl.drafts import (
    AttributeDraft,
    AttributeGroupDraft,
    ProcessRunDraft,
    ProcessRunStepDraft,
    ProcessTemplateDraft,
    ResourceSlotDraft,
    StepTemplateDraft,
)
from recap.dsl.query import QuerySpec
from recap.exceptions import (
    ExistingProcessRunError,
    ExistingProcessRunWarning,
    ExistingProcessTemplateError,
    ExistingProcessTemplateWarning,
)
from recap.lifecycle import LifecycleStatus
from recap.schemas.attribute import AttributeTemplateValidator
from recap.schemas.process import (
    ProcessRunSchema,
    ProcessTemplateRef,
    ProcessTemplateSchema,
)
from recap.schemas.resource import ResourceSchema
from recap.schemas.step import StepSchema, StepTemplateRef
from recap.utils.dsl import lock_instance_fields
from recap.utils.general import Direction


class ProcessTemplateBuilder:
    """Builder for process templates, resource slots, and step templates.

    Context-manager exit commits clean work and rolls back exceptions. Existing
    templates load by UUID; ``on_existing`` controls identity reuse, while draft
    validation reports invalid slots, steps, or parameters before submission.
    """

    def __init__(  # noqa: C901
        self,
        namespace_id: UUID,
        name: str | None,
        version: str | None,
        *,
        backend: ClientBackend,
        process_template_id: UUID | None = None,
        on_existing: Literal["silent", "warn", "raise"] = "warn",
        namespace_path: str | None = None,
        command_context: CommandContext | None = None,
    ):
        if command_context is None:
            raise ValueError("ProcessTemplateBuilder requires command context")
        self.backend = backend
        self.namespace_id = namespace_id
        self.namespace_path = namespace_path
        self._command_context = command_context
        self._submitted = False
        self._last_draft = None
        self._expected_revision = 1
        self._draft_labels: list[str] = []
        self._draft_resource_slots: list[ResourceSlotDraft] = []
        self._draft_steps: list[StepTemplateBuilder] = []
        if on_existing not in {"silent", "warn", "raise"}:
            raise ValueError("on_existing must be one of: 'silent', 'warn', 'raise'")
        self.on_existing = on_existing
        if namespace_path is None:
            raise ValueError("namespace_path is required for command-backed builders")
        self.name = name
        self.version = version
        self._template: ProcessTemplateRef | None = None
        self._resource_slots = {}
        self._current_step_builder = None
        if process_template_id is not None:
            self._initialize_command_update(process_template_id)
        elif name is None or version is None:
            raise ValueError("name and version are required to create a process template")
        else:
            existing = self.backend.query(
                ProcessTemplateSchema,
                QuerySpec(
                    filters={"name": name, "version": version},
                    preloads=["step_templates", "resource_slots"],
                    include_mutable=True,
                ),
                namespace_path=namespace_path,
            )
            if existing:
                if on_existing == "raise":
                    raise ExistingProcessTemplateError(
                        f"Process template {name!r} version {version!r} already exists"
                    )
                if on_existing == "warn":
                    warnings.warn(
                        f"Process template {name!r} version {version!r} already exists and will be reused; bump the version",
                        ExistingProcessTemplateWarning,
                        stacklevel=2,
                    )
                self._template = existing[0]
                self._expected_revision = existing[0].revision
                self._initialize_command_update(existing[0].id)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None and not self._submitted:
            self.save()

    def save(self):
        """Validate and persist current process-template draft."""
        draft = self._build_draft()
        if self._submitted and draft == self._last_draft:
            return self
        command = (
            CreateProcessTemplate(namespace_path=self.namespace_path, draft=draft)
            if self._template is None
            else UpdateProcessTemplate(
                template_id=self._template.id,
                expected_revision=self._expected_revision,
                draft=draft,
            )
        )
        result = self.backend._execute(command, self._command_context)
        if result is not None:
            self._template = result
            self._expected_revision = result.revision
        self._last_draft = draft
        self._submitted = True
        return self

    def activate(self):
        """Transition process template to ACTIVE."""
        self._ensure_template()
        if self._template is None:
            self.save()
        self._template = self.backend._execute(
            SetLifecycleStatus(
                object_type="process_template",
                object_id=self.template.id,
                expected_revision=self._template.revision,
                status=LifecycleStatus.ACTIVE.value,
            ),
            self._command_context,
        )
        return self

    def archive(self):
        """Transition process template to ARCHIVED."""
        self._ensure_template()
        if self._template is None:
            self.save()
        self._template = self.backend._execute(
            SetLifecycleStatus(
                object_type="process_template",
                object_id=self.template.id,
                expected_revision=self._template.revision,
                status=LifecycleStatus.ARCHIVED.value,
            ),
            self._command_context,
        )
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
        self.save()

    def add_resource_slot(
        self,
        name: str,
        resource_type: str,
        direction: Direction,
        create_resource_type=False,
        required: bool = True,
    ) -> "ProcessTemplateBuilder":
        """Add typed input/output resource slot after validating direction."""
        existing = next(
            (slot for slot in self._draft_resource_slots if slot.name == name), None
        )
        if existing is not None:
            if existing.resource_type != resource_type or existing.direction != direction:
                raise ValueError(
                    f"ResourceSlot {name} already exists with different type/direction"
                )
            return self
        self._draft_resource_slots.append(
            ResourceSlotDraft(
                name=name,
                resource_type=resource_type,
                direction=direction,
                create_resource_type=create_resource_type,
                required=required,
            )
        )
        return self

    def add_step(
        self,
        name: str,
    ):
        """Open builder for a named process step."""
        existing = next(
            (step for step in self._draft_steps if step._draft_name == name), None
        )
        if existing is not None:
            return existing
        builder = StepTemplateBuilder(parent=self, draft_name=name)
        self._draft_steps.append(builder)
        return builder

    def get_model(self, *, update: bool = False) -> ProcessTemplateSchema:
        """
        Return a pydantic model for the process template, optionally reloading
        from the backend first. Critical fields are locked against mutation.
        """
        if update or self._template is None:
            self.save()
        if not isinstance(self._template, ProcessTemplateSchema):
            raise RuntimeError("Command backend did not return process template")
        return lock_instance_fields(
            self._template.model_copy(deep=True),
            {"id", "create_date", "modified_date", "version"},
        )

    def set_model(self, model: ProcessTemplateSchema | ProcessTemplateRef):
        """Replace working template after verifying UUID identity."""
        if self._template is None:
            raise RuntimeError("Template not initialized")
        if model.id != self._template.id:
            raise ValueError("ID for this ProcessTemplate does not match the builder")
        self._template = model

    def _build_draft(self) -> ProcessTemplateDraft:
        return ProcessTemplateDraft(
            name=self.name,
            version=self.version,
            labels=self._draft_labels,
            resource_slots=self._draft_resource_slots,
            steps=[step._build_draft() for step in self._draft_steps],
        )

    def _initialize_command_update(self, process_template_id: UUID) -> None:
        templates = self.backend.query(
            ProcessTemplateSchema,
            QuerySpec(
                filters={"id": process_template_id},
                preloads=["step_templates", "resource_slots"],
                include_mutable=True,
            ),
            namespace_path=self.namespace_path,
        )
        if not templates:
            raise ValueError(f"ProcessTemplate with id {process_template_id} not found")
        template = templates[0]
        self._template = template
        self.name = template.name
        self.version = template.version
        self._expected_revision = template.revision
        self._draft_labels = list(template.labels)
        self._draft_resource_slots = [
            ResourceSlotDraft(
                name=slot.name,
                resource_type=slot.resource_type.name,
                direction=slot.direction,
                required=slot.required,
            )
            for slot in template.resource_slots
        ]
        for step in template.step_templates.values():
            step_builder = StepTemplateBuilder(parent=self, draft_name=step.name)
            step_builder._draft_role_bindings.update(
                {role: slot.name for role, slot in step.resource_slots.items()}
            )
            for group in step.attribute_group_templates:
                group_builder = DraftAttributeGroupBuilder(group.name, step_builder)
                group_builder._attributes.extend(
                    AttributeDraft(
                        name=attribute.name,
                        type=attribute.value_type,
                        unit=attribute.unit or "",
                        default=attribute.default_value,
                        metadata=attribute.metadata or {},
                    )
                    for attribute in group.attribute_templates
                )
                step_builder._draft_parameter_groups.append(group_builder)
            self._draft_steps.append(step_builder)

class StepTemplateBuilder:
    """Scoped editor for one step's parameter groups and resource bindings."""

    def __init__(  # noqa: C901
        self,
        parent: ProcessTemplateBuilder,
        step_template: StepTemplateRef | None = None,
        draft_name: str | None = None,
    ):
        self.parent: ProcessTemplateBuilder = parent
        self._draft_name = draft_name
        self._draft_role_bindings: dict[str, str] = {}
        self._draft_parameter_groups: list[DraftAttributeGroupBuilder] = []
        self._template = step_template

    def close_step(self) -> ProcessTemplateBuilder:
        """Return owning process-template builder."""
        return self.parent

    def param_group(
        self, group_name: str
    ) -> "AttributeGroupBuilder[StepTemplateBuilder]":
        """Open parameter-group builder for this step."""
        existing = next(
            (
                group
                for group in self._draft_parameter_groups
                if group.group_name == group_name
            ),
            None,
        )
        if existing is not None:
            return existing
        builder = DraftAttributeGroupBuilder(group_name, self)
        self._draft_parameter_groups.append(builder)
        return builder

    def bind_slot(self, role: str, slot_name: str):
        """Bind process resource slot to step role."""
        self._draft_role_bindings[role] = slot_name
        return self

    def _build_draft(self) -> StepTemplateDraft:
        return StepTemplateDraft(
            name=self._draft_name,
            role_bindings=self._draft_role_bindings,
            parameter_groups=[
                group._build_draft() for group in self._draft_parameter_groups
            ],
        )

    def add_parameters(self, param_def: dict[str, list[dict[str, Any]]]):
        """Validate and add grouped parameter definitions."""
        for group_key, params in param_def.items():
            group = self.param_group(group_key)
            for param in params:
                attribute = AttributeTemplateValidator.model_validate(param)
                group.add_attribute(
                    attribute.name,
                    attribute.type,
                    attribute.unit,
                    attribute.default,
                    metadata=attribute.metadata,
                )
            group.close_group()
        return self


class DraftAttributeGroupBuilder:
    """Draft-only builder for step parameter definitions."""

    def __init__(self, group_name: str, parent: StepTemplateBuilder):
        self.group_name = group_name
        self.parent = parent
        self._attributes: list[AttributeDraft] = []

    def add_attribute(
        self,
        attr_name: str,
        value_type: str,
        unit: str,
        default: Any,
        metadata: dict[str, Any] | None = None,
    ) -> "DraftAttributeGroupBuilder":
        existing = next(
            (item for item in self._attributes if item.name == attr_name), None
        )
        if existing is not None:
            return self
        self._attributes.append(
            AttributeDraft(
                name=attr_name,
                type=value_type,
                unit=unit,
                default=default,
                metadata=metadata or {},
            )
        )
        return self

    def close_group(self) -> StepTemplateBuilder:
        return self.parent

    def _build_draft(self) -> AttributeGroupDraft:
        return AttributeGroupDraft(name=self.group_name, attributes=self._attributes)


class ProcessRunBuilder:
    """Builder for process-run assignments, parameters, and child steps.

    Clean context-manager exit commits and exception exit rolls back local work;
    command-backed remote builders submit validated drafts. UUID loading,
    ``on_existing``, lifecycle methods, and validation errors are preserved.
    """

    def __init__(  # noqa: C901
        self,
        name: str | None,
        description: str | None,
        template_name: str | None,
        namespace_id: UUID | None,
        version: str | None = None,
        *,
        backend: ClientBackend,
        process_run_id: UUID | None = None,
        on_existing: Literal["silent", "warn", "raise"] = "warn",
        namespace_path: str | None = None,
        command_context: CommandContext | None = None,
        template_id: UUID | None = None,
    ):
        if command_context is None:
            raise ValueError("ProcessRunBuilder requires command context")
        self.backend = backend
        if namespace_id is None:
            raise ValueError("Namespace context is required")
        self.namespace_id = namespace_id
        self.name = name
        self.description = description
        self.template_name = template_name
        self.version = version
        if on_existing not in {"silent", "warn", "raise"}:
            raise ValueError("on_existing must be one of: 'silent', 'warn', 'raise'")
        self.on_existing = on_existing
        self.namespace_path = namespace_path
        self._command_context = command_context
        self._template_id = template_id
        self._process_template = None
        self._submitted = False
        self._draft_assignments: dict[str, UUID] = {}
        self._draft_steps: dict[str, dict[str, dict[str, object]]] = {}
        if namespace_path is None or (
            template_id is None
            and process_run_id is None
            and (template_name is None or version is None)
        ):
            raise ValueError(
                "namespace_path and template_id or process_run_id are "
                "required for command-backed builders"
            )
        self._process_run = (
            self._reload_process_run(process_run_id) if process_run_id is not None else None
        )
        self._dirty = False
        if self._process_run is not None:
            self.name = self._process_run.name
            self.description = self._process_run.description
            self._template_id = self._process_run.template.id
        filters = (
            {"id": self._template_id}
            if self._template_id is not None
            else {"name": self.template_name, "version": self.version}
        )
        templates = self.backend.query(
            ProcessTemplateSchema,
            QuerySpec(
                filters=filters,
                preloads=["step_templates", "resource_slots"],
                include_mutable=True,
            ),
            namespace_path=namespace_path,
        )
        if not templates:
            raise ValueError(
                f"Process template {self.template_name!r} version "
                f"{self.version!r} not found"
            )
        self._process_template = templates[0]
        self._template_id = self._process_template.id
        if self._process_run is None:
            existing_runs = self.backend.query(
                ProcessRunSchema,
                QuerySpec(
                    filters={"name": name},
                    preloads=["steps", "steps.parameters", "resources"],
                    include_mutable=True,
                ),
                namespace_path=namespace_path,
            )
            if existing_runs:
                if on_existing == "raise":
                    raise ExistingProcessRunError(
                        f"Process run {name!r} already exists"
                    )
                if on_existing == "warn":
                    warnings.warn(
                        f"Process run {name!r} already exists and will be reused",
                        ExistingProcessRunWarning,
                        stacklevel=2,
                    )
                self._process_run = existing_runs[0]
                self.name = self._process_run.name
                self.description = self._process_run.description
        self._steps = (
            list(self._process_run.steps.values()) if self._process_run is not None else []
        )
        self._resources = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.save()

    def save(self):
        """Validate and persist current process-run changes."""
        if self._submitted and not self._dirty:
            return self
        if self._process_run is None:
            command = CreateProcessRun(
                namespace_path=self.namespace_path,
                draft=ProcessRunDraft(
                    name=self.name,
                    description=self.description,
                    template_id=self._template_id,
                    assignments=self._draft_assignments,
                    steps={
                        step.name: ProcessRunStepDraft()
                        for step in (
                            getattr(self._process_template, "step_templates", None) or {}
                        ).values()
                    },
                ),
            )
        else:
            command = UpdateProcessRun(
                process_run_id=self._process_run.id,
                expected_revision=self._process_run.revision,
                description=self.description,
                assignments=self._draft_assignments or None,
                steps=self._draft_steps or None,
            )
        result = self.backend._execute(command, self._command_context)
        self._process_run = result
        if result is not None and hasattr(result, "steps"):
            self._steps = list(result.steps.values())
        self._dirty = False
        self._submitted = True
        return self

    def finalize(self):
        """Transition process run to ACTIVE/finalized state."""
        self._ensure_command_saved()
        self._process_run = self.backend._execute(
            UpdateProcessRun(
                process_run_id=self._process_run.id,
                expected_revision=self._process_run.revision,
                status=LifecycleStatus.ACTIVE.value,
            ),
            self._command_context,
        )
        return self

    def archive(self):
        """Transition process run to ARCHIVED."""
        self._ensure_command_saved()
        self._process_run = self.backend._execute(
            UpdateProcessRun(
                process_run_id=self._process_run.id,
                expected_revision=self._process_run.revision,
                status=LifecycleStatus.ARCHIVED.value,
            ),
            self._command_context,
        )
        return self

    @property
    def process_run(self) -> ProcessRunSchema:
        if self._process_run is None:
            self.save()
        return self._process_run

    def set_model(self, model: ProcessRunSchema):
        """Replace working run model after verifying UUID identity."""
        if self._process_run is None:
            raise RuntimeError("ProcessRun not initialized")
        if model.id != self._process_run.id:
            raise ValueError("ID for this ProcessRun does not match the builder")
        self._process_run = model
        self.description = model.description
        self._draft_steps = self._model_steps_payload(model)
        self._dirty = True

    def assign_resource(
        # self, resource_slot_name: str, resource_name: str, resource_template_name: str
        self,
        resource_slot_name: str,
        resource: ResourceSchema,
    ) -> "ProcessRunBuilder":
        """Assign resource to named process slot."""
        self._draft_assignments[resource_slot_name] = resource.id
        self._dirty = True
        return self

    @property
    def steps(self) -> list[StepSchema]:
        if self._process_run is not None and not self._steps:
            self._steps = list(
                self._reload_process_run(self._process_run.id).steps.values()
            )
        return self._steps

    def get_params(  # noqa: C901
        self,
        step_name: str | None = None,
        step_schema: StepSchema | None = None,
    ) -> type[BaseModel]:
        """Return typed parameter model for selected process step."""
        if not self._steps:
            self._steps = list(
                self._reload_process_run(self.process_run.id).steps.values()
            )
        if step_name is None and step_schema is None:
            raise ValueError("Provide step_name or step_schema to get params")
        if not step_schema and step_name:
            for step in self._steps:
                if step.name == step_name:
                    step_schema = step
                    break

        if step_schema is None:
            raise NoResultFound("Step not found with name: {step_name} ")
        required_slots = {
            slot.name for slot in self._process_template.resource_slots if slot.required
        }
        assigned_slots = set(self._draft_assignments)
        if self._process_run is not None:
            assigned_slots.update(self._process_run.assigned_resources)
        missing_slots = required_slots - assigned_slots
        if missing_slots:
            raise ValueError(
                "Process run is missing resources for slots: "
                + ", ".join(sorted(missing_slots))
            )
        parameters = step_schema.parameters
        model = create_model(
            step_schema.name,
            __base__=type(parameters),
            step_name=(Literal[step_schema.name], step_schema.name),
            step_id=(UUID, step_schema.id),
        )
        return model.model_validate(
            {
                **{name: getattr(parameters, name) for name in type(parameters).model_fields},
                "step_name": step_schema.name,
                "step_id": step_schema.id,
            }
        )

    def set_params(self, filled_params: type[BaseModel]):
        """Validate and apply typed parameter values in current transaction."""
        step_data: dict[str, dict[str, object]] = {}
        for field_name, field_info in type(filled_params).model_fields.items():
            if field_name in {"step_name", "step_id"}:
                continue
            group = getattr(filled_params, field_name)
            if not isinstance(group, BaseModel):
                continue
            value_model = getattr(group, "values", group)
            values = {}
            for attr_name, attr_info in type(value_model).model_fields.items():
                value = getattr(value_model, attr_name)
                if not hasattr(value, "value"):
                    continue
                values[attr_info.alias or attr_name] = {"value": value.value, "unit": value.unit}
            step_data[field_info.alias or field_name] = values
        self._draft_steps[filled_params.step_name] = step_data
        self._dirty = True
        return self

    @staticmethod
    def _model_steps_payload(model: ProcessRunSchema):
        result = {}
        for step in model.steps.values():
            groups = {}
            if not isinstance(step.parameters, BaseModel):
                result[step.name] = groups
                continue
            for group_name, group_info in type(step.parameters).model_fields.items():
                group = getattr(step.parameters, group_name)
                value_model = getattr(group, "values", group)
                groups[group_info.alias or group_name] = {
                    attr_info.alias or attr_name: {
                        "value": getattr(value_model, attr_name).value,
                        "unit": getattr(value_model, attr_name).unit,
                    }
                    for attr_name, attr_info in type(value_model).model_fields.items()
                    if hasattr(getattr(value_model, attr_name), "value")
                }
            result[step.name] = groups
        return result

    def get_model(self, *, update: bool = False) -> ProcessRunSchema:
        """
        Return a pydantic model representing the process run, optionally reloading
        from the backend first. Critical fields are locked against mutation.
        """
        if update:
            self._process_run = self._reload_process_run(self._process_run.id)
        model = self._process_run.model_copy(deep=True)
        return lock_instance_fields(
            model, {"id", "create_date", "modified_date", "template"}
        )

    def _ensure_command_saved(self):
        if not self._submitted:
            self.save()

    def _reload_process_run(self, process_run_id: UUID) -> ProcessRunSchema:
        runs = self.backend.query(
            ProcessRunSchema,
            QuerySpec(
                filters={"id": process_run_id},
                preloads=["steps", "steps.parameters", "resources"],
                include_mutable=True,
            ),
            namespace_path=self.namespace_path,
        )
        if not runs:
            raise ValueError(f"ProcessRun with id {process_run_id} not found")
        return runs[0]
