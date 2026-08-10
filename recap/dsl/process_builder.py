import warnings
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.exc import NoResultFound

from recap.adapter import Backend
from recap.commands.models import (
    CommandContext,
    CreateProcessRun,
    CreateProcessTemplate,
    UpdateProcessRun,
    UpdateProcessTemplate,
)
from recap.dsl.attribute_builder import AttributeGroupBuilder
from recap.dsl.drafts import (
    AttributeDraft,
    AttributeGroupDraft,
    ProcessRunDraft,
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
from recap.schemas.resource import ResourceSchema, ResourceSlotSchema
from recap.schemas.step import StepSchema, StepTemplateRef
from recap.utils.dsl import lock_instance_fields
from recap.utils.general import Direction


class ProcessTemplateBuilder:
    """Builder for process templates, resource slots, and step templates.

    Context-manager exit commits clean work and rolls back exceptions. Existing
    templates load by UUID; ``on_existing`` controls identity reuse, while draft
    validation reports invalid slots, steps, or parameters before submission.
    """

    def __init__(
        self,
        backend: Backend,
        namespace_id: UUID,
        name: str | None,
        version: str | None,
        process_template_id: UUID | None = None,
        on_existing: Literal["silent", "warn", "raise"] = "warn",
        namespace_path: str | None = None,
        command_context: CommandContext | None = None,
    ):
        self.backend = backend
        self.namespace_id = namespace_id
        self.namespace_path = namespace_path
        self._command_context = command_context
        self._command_mode = command_context is not None
        self._submitted = False
        self._expected_revision = 1
        self._draft_labels: list[str] = []
        self._draft_resource_slots: list[ResourceSlotDraft] = []
        self._draft_steps: list[StepTemplateBuilder] = []
        self._uow = None
        if on_existing not in {"silent", "warn", "raise"}:
            raise ValueError("on_existing must be one of: 'silent', 'warn', 'raise'")
        self.on_existing = on_existing
        if self._command_mode:
            if namespace_path is None:
                raise ValueError(
                    "namespace_path is required for command-backed builders"
                )
            self.name = name
            self.version = version
            self._template: ProcessTemplateRef | None = None
            self._resource_slots = {}
            self._current_step_builder = None
            if process_template_id is not None:
                self._initialize_command_update(process_template_id)
            elif name is None or version is None:
                raise ValueError(
                    "name and version are required to create a process template"
                )
            return
        try:
            self._ensure_uow()
            self.name = name
            self.version = version
            self._template: ProcessTemplateRef | None = None
            self._resource_slots: dict[str, ResourceSlotSchema] = {}
            self._current_step_builder: StepTemplateBuilder | None = None
            self._initialize_template(process_template_id)
        except Exception:
            if self._uow:
                self._uow.rollback()
                self._uow = None
            raise

    def _initialize_template(self, process_template_id: UUID | None):
        if process_template_id is not None:
            tmpl = self.backend.get_process_template(
                self.namespace_id,
                name=None,
                version=None,
                id=process_template_id,
                expand=False,
            )
            self.name = tmpl.name
            self.version = tmpl.version
            self._template = tmpl
            return
        if self.name is None or self.version is None:
            raise ValueError(
                "name and version are required to create a process template"
            )

    def __enter__(self):
        if self._command_mode:
            return self
        self._ensure_uow()
        if self._template is not None:
            self._reload_template()
            self.name = self._template.name
            self.version = self._template.version
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._command_mode:
            if exc_type is None:
                self.save()
            return
        if exc_type is None:
            self.save()
        else:
            if self._uow:
                self._uow.rollback()
            self._uow = None

    def save(self):
        """Validate and persist current process-template draft."""
        if self._command_mode:
            draft = self._build_draft()
            if self._template is None:
                command = CreateProcessTemplate(
                    namespace_path=self.namespace_path, draft=draft
                )
            else:
                command = UpdateProcessTemplate(
                    template_id=self._template.id,
                    expected_revision=self._expected_revision,
                    draft=draft,
                )
            result = self.backend.execute(command, self._command_context)
            if result is not None:
                self._template = result
                self._expected_revision = result.revision
            self._submitted = True
            return self
        self._ensure_uow()
        self._uow.commit()
        self._uow = None
        return self

    def activate(self):
        """Transition process template to ACTIVE."""
        self._ensure_template()
        self.backend.set_process_template_status(
            self.template.id, LifecycleStatus.ACTIVE
        )
        return self

    def archive(self):
        """Transition process template to ARCHIVED."""
        self._ensure_template()
        self.backend.set_process_template_status(
            self.template.id, LifecycleStatus.ARCHIVED
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
        self._ensure_uow()
        if self._template:
            return
        if self.name is None or self.version is None:
            raise ValueError(
                "name and version are required to create a process template"
            )
        try:
            created = self.backend.create_process_template(
                self.namespace_id, self.name, self.version
            )
            if created is None:
                raise ValueError("Process template already exists")
            self._template = created
        except Exception as exc:
            if self.on_existing == "raise":
                raise ExistingProcessTemplateError(
                    f"Process template {self.name!r} version {self.version!r} already exists"
                ) from exc
            self._restart_uow()
            self._template = self.backend.get_process_template(
                self.namespace_id, self.name, self.version, expand=False
            )
            if self.on_existing == "warn":
                warnings.warn(
                    (
                        f"Process template {self.name!r} version {self.version!r} already "
                        "exists and will be reused; no new template will be created. "
                        "If you want a new template, bump the version."
                    ),
                    ExistingProcessTemplateWarning,
                    stacklevel=2,
                )

    def _reload_template(self):
        self._ensure_uow()
        self._template = self.backend.get_process_template(
            self.namespace_id, self.name, self.version, expand=True
        )

    def add_resource_slot(
        self,
        name: str,
        resource_type: str,
        direction: Direction,
        create_resource_type=False,
        required: bool = True,
    ) -> "ProcessTemplateBuilder":
        """Add typed input/output resource slot after validating direction."""
        if self._command_mode:
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
        self._ensure_uow()
        self._ensure_template()
        self._resource_slots[name] = self.backend.add_resource_slot(
            name,
            resource_type,
            direction,
            self.template,
            create_resource_type,
            required=required,
        )
        return self

    def add_step(
        self,
        name: str,
    ):
        """Open builder for a named process step."""
        if self._command_mode:
            builder = StepTemplateBuilder(parent=self, draft_name=name)
            self._draft_steps.append(builder)
            return builder
        self._ensure_uow()
        self._ensure_template()
        step_template = self.backend.add_step(name, self.template)
        step_template_builder = StepTemplateBuilder(
            parent=self, step_template=step_template
        )
        return step_template_builder

    def get_model(self, *, update: bool = False) -> ProcessTemplateSchema:
        """
        Return a pydantic model for the process template, optionally reloading
        from the backend first. Critical fields are locked against mutation.
        """
        self._ensure_uow()
        if update:
            self._reload_template()
        elif self._template is None:
            self._ensure_template()

        model = self.backend.get_process_template(
            self.namespace_id, self.name, self.version, expand=True
        )
        return lock_instance_fields(
            model.model_copy(deep=True),
            {"id", "create_date", "modified_date", "version"},
        )

    def set_model(self, model: ProcessTemplateSchema | ProcessTemplateRef):
        """Replace working template after verifying UUID identity."""
        if self._template is None:
            raise RuntimeError("Template not initialized")
        if model.id != self._template.id:
            raise ValueError("ID for this ProcessTemplate does not match the builder")
        self._template = model

    def _ensure_uow(self):
        if self._uow is None:
            self._uow = self.backend.begin()
        return self._uow

    def _build_draft(self) -> ProcessTemplateDraft:
        return ProcessTemplateDraft(
            name=self.name,
            version=self.version,
            labels=self._draft_labels,
            resource_slots=self._draft_resource_slots,
            steps=[step._build_draft() for step in self._draft_steps],
        )

    def _initialize_command_update(self, process_template_id: UUID) -> None:
        template = self.backend.get_process_template(
            self.namespace_id,
            name=None,
            version=None,
            id=process_template_id,
            expand=True,
        )
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

    def _restart_uow(self):
        if self._uow:
            self._uow.rollback()
        self._uow = self.backend.begin()
        return self._uow


class StepTemplateBuilder:
    """Scoped editor for one step's parameter groups and resource bindings."""

    def __init__(
        self,
        parent: ProcessTemplateBuilder,
        step_template: StepTemplateRef | None = None,
        draft_name: str | None = None,
    ):
        self.parent: ProcessTemplateBuilder = parent
        self.backend: Backend = parent.backend
        self._draft_name = draft_name
        self._draft_role_bindings: dict[str, str] = {}
        self._draft_parameter_groups: list[DraftAttributeGroupBuilder] = []
        self.process_template = None if parent._command_mode else parent.template
        self._template = step_template
        self._bound_slots = {}

    def close_step(self) -> ProcessTemplateBuilder:
        """Return owning process-template builder."""
        return self.parent

    def param_group(
        self, group_name: str
    ) -> "AttributeGroupBuilder[StepTemplateBuilder]":
        """Open parameter-group builder for this step."""
        if self.parent._command_mode:
            builder = DraftAttributeGroupBuilder(group_name, self)
            self._draft_parameter_groups.append(builder)
            return builder
        attr_group_builder: AttributeGroupBuilder[StepTemplateBuilder] = (
            AttributeGroupBuilder(group_name=group_name, parent=self)
        )
        return attr_group_builder

    def bind_slot(self, role: str, slot_name: str):
        """Bind process resource slot to step role."""
        if self.parent._command_mode:
            self._draft_role_bindings[role] = slot_name
            return self
        slot = self.backend.bind_slot(
            role, slot_name, self.process_template, self._template
        )
        self._bound_slots[slot.name] = slot
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

    def __init__(
        self,
        name: str | None,
        description: str | None,
        template_name: str | None,
        namespace_id: UUID | None,
        backend: Backend,
        version: str | None = None,
        process_run_id: UUID | None = None,
        on_existing: Literal["silent", "warn", "raise"] = "warn",
        namespace_path: str | None = None,
        command_context: CommandContext | None = None,
        template_id: UUID | None = None,
    ):
        self.backend = backend
        if namespace_id is None:
            raise ValueError("Namespace context is required")
        self.namespace_id = namespace_id
        self._uow = None
        self.name = name
        self.description = description
        self.template_name = template_name
        self.version = version
        if on_existing not in {"silent", "warn", "raise"}:
            raise ValueError("on_existing must be one of: 'silent', 'warn', 'raise'")
        self.on_existing = on_existing
        self.namespace_path = namespace_path
        self._command_context = command_context
        self._command_mode = command_context is not None
        self._template_id = template_id
        self._submitted = False
        self._draft_assignments: dict[str, UUID] = {}
        if self._command_mode:
            if namespace_path is None or (
                template_id is None and process_run_id is None
            ):
                raise ValueError(
                    "namespace_path and template_id or process_run_id are required for command-backed builders"
                )
            self._process_run = (
                self._reload_process_run(process_run_id)
                if process_run_id is not None
                else None
            )
            if self._process_run is not None:
                self.name = self._process_run.name
                self.description = self._process_run.description
                self._template_id = self._process_run.template.id
            self._steps = []
            self._resources = {}
            return
        self._process_template: ProcessTemplateSchema | ProcessTemplateRef | None = None
        self._loaded_in_uow: bool = False
        self._model_dirty: bool = False
        self._params_flushed: bool = False
        try:
            self._initialize_process_run(process_run_id)
            self._loaded_in_uow = True  # mark run as fresh in this UoW
            self._steps = list(self._process_run.steps.values())
            self._resources = {}
        except Exception:
            if self._uow:
                self._uow.rollback()
                self._uow = None
            raise

    def _initialize_process_run(
        self,
        process_run_id: UUID | None,
    ):
        self._ensure_uow()
        if process_run_id is not None:
            self._load_existing_process_run(process_run_id)
            return
        self._validate_new_process_run_inputs()
        self._process_template = self.backend.get_process_template(
            self.namespace_id, self.template_name, self.version, expand=True
        )
        try:
            self._process_run = self.backend.create_process_run(
                self.namespace_id,
                self.name,
                self.description,
                self._process_template,
            )
        except Exception as exc:
            self._handle_existing_process_run(exc)

    def _load_existing_process_run(self, process_run_id: UUID):
        self._process_run = self._reload_process_run(process_run_id)
        template = self._process_run.template
        self._process_template = self.backend.get_process_template(
            self.namespace_id, template.name, template.version, expand=True
        )
        self.name = self._process_run.name
        self.description = self._process_run.description
        self.template_name = template.name
        self.version = template.version

    def _validate_new_process_run_inputs(self):
        if (
            self.name is None
            or self.description is None
            or self.template_name is None
            or self.version is None
        ):
            raise ValueError(
                "name, description, template_name, and version are required to create a process run"
            )

    def _handle_existing_process_run(self, create_error: Exception):
        self._restart_uow()
        existing = self.backend.query(
            ProcessRunSchema,
            QuerySpec(
                filters={"name": self.name},
                preloads=["steps", "steps.parameters", "resources"],
                include_mutable=True,
            ),
            namespace_path=self.backend.get_namespace_path(self.namespace_id),
        )
        if self.on_existing == "raise":
            raise ExistingProcessRunError(
                f"Process run {self.name!r} already exists"
            ) from create_error
        if not existing:
            raise create_error
        if self.on_existing == "warn":
            warnings.warn(
                (
                    f"Process run {self.name!r} already exists and will be reused; "
                    "no new run will be created."
                ),
                ExistingProcessRunWarning,
                stacklevel=2,
            )
        self._process_run = existing[0]
        self._loaded_in_uow = True  # mark fresh after recovery load

    def __enter__(self):
        if self._command_mode:
            return self
        self._ensure_uow()
        if getattr(self, "_process_run", None) is not None and not self._loaded_in_uow:
            # Re-entering after save() or _restart_uow() — reload current state
            self._process_run = self._reload_process_run(self._process_run.id)
            template = self._process_run.template
            self._process_template = self.backend.get_process_template(
                self.namespace_id, template.name, template.version, expand=True
            )
            self.name = self._process_run.name
            self.description = self._process_run.description
            self.template_name = template.name
            self.version = template.version
            self._steps = list(self._process_run.steps.values())
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._command_mode:
            if exc_type is None:
                self.save()
            return
        if exc_type is None:
            if self._model_dirty:
                if self._params_flushed:
                    # set_params() flushed param values directly to the ORM, but
                    # the in-memory pydantic schema still holds the pre-flush
                    # values. Reload so persist() does not overwrite the flushed
                    # params with stale schema data. (Only needed when a model
                    # mutation is combined with set_params() in the same block.)
                    self._process_run = self._reload_process_run(self._process_run.id)
                self.persist()
            self.save()
        else:
            if self._uow:
                self._uow.rollback()
            self._uow = None

    def save(self):
        """Validate and persist current process-run changes."""
        if self._command_mode:
            if self._process_run is None:
                command = CreateProcessRun(
                    namespace_path=self.namespace_path,
                    draft=ProcessRunDraft(
                        name=self.name,
                        description=self.description,
                        template_id=self._template_id,
                        assignments=self._draft_assignments,
                    ),
                )
            else:
                command = UpdateProcessRun(
                    process_run_id=self._process_run.id,
                    expected_revision=self._process_run.revision,
                    description=self.description,
                    assignments=self._draft_assignments or None,
                )
            result = self.backend.execute(command, self._command_context)
            self._process_run = result
            self._submitted = True
            return self
        self._ensure_uow()
        self._uow.commit()
        self._loaded_in_uow = False
        self._model_dirty = False
        self._params_flushed = False
        self._uow = None
        return self

    def finalize(self):
        """Transition process run to ACTIVE/finalized state."""
        if self._command_mode:
            self._ensure_command_saved()
            self._process_run = self.backend.execute(
                UpdateProcessRun(
                    process_run_id=self._process_run.id,
                    expected_revision=self._process_run.revision,
                    status=LifecycleStatus.ACTIVE.value,
                ),
                self._command_context,
            )
            return self
        self._ensure_uow()
        self.backend.set_process_run_status(
            self._process_run.id, LifecycleStatus.ACTIVE
        )
        return self

    def archive(self):
        """Transition process run to ARCHIVED."""
        self._ensure_uow()
        self.backend.set_process_run_status(
            self._process_run.id, LifecycleStatus.ARCHIVED
        )
        return self

    def persist(self):
        """Write current process-run model changes."""
        self._process_run = self.backend.update_process_run(self._process_run)
        return self

    @property
    def process_run(self) -> ProcessRunSchema:
        return self._process_run

    def set_model(self, model: ProcessRunSchema):
        """Replace working run model after verifying UUID identity."""
        if self._process_run is None:
            raise RuntimeError("ProcessRun not initialized")
        if model.id != self._process_run.id:
            raise ValueError("ID for this ProcessRun does not match the builder")
        self._process_run = model
        self._model_dirty = True  # pydantic schema mutated; persist needed

    def assign_resource(
        # self, resource_slot_name: str, resource_name: str, resource_template_name: str
        self,
        resource_slot_name: str,
        resource: ResourceSchema,
    ) -> "ProcessRunBuilder":
        """Assign resource to named process slot."""
        if self._command_mode:
            self._draft_assignments[resource_slot_name] = resource.id
            return self
        self._ensure_uow()
        resource_slot = None
        for slot in self._process_template.resource_slots:
            if slot.name == resource_slot_name:
                resource_slot = slot
                break
        # resource = self.backend.get_resource(resource_name, resource_template_name)
        if resource_slot is None:
            raise NoResultFound(f"Resource slot {resource_slot_name} not found")
        self._process_run = self.backend.assign_resource(
            resource_slot, resource, self._process_run
        )
        self._model_dirty = True  # ORM updated and schema refreshed; persist needed
        return self

    def _check_resource_assignment(self):
        self._ensure_uow()
        self.backend.check_resource_assignment(self._process_template, self.process_run)

    @property
    def steps(self) -> list[StepSchema]:
        self._ensure_uow()
        self._check_resource_assignment()
        if self._steps is None:
            self._steps = self.backend.get_steps(self.process_run)
        return self._steps

    def get_params(
        self,
        step_name: str | None = None,
        step_schema: StepSchema | None = None,
    ) -> type[BaseModel]:
        """Return typed parameter model for selected process step."""
        self._ensure_uow()
        if step_name is None and step_schema is None:
            raise ValueError("Provide step_name or step_schema to get params")
        if not step_schema and step_name:
            for step in self.steps:
                if step.name == step_name:
                    step_schema = step
                    break

        if step_schema is None:
            raise NoResultFound("Step not found with name: {step_name} ")
        self._check_resource_assignment()
        return self.backend.get_params(step_schema)

    def set_params(self, filled_params: type[BaseModel]):
        """Validate and apply typed parameter values in current transaction."""
        self._ensure_uow()
        self.backend.set_params(filled_params)
        # backend.set_params() already flushed the values to the session within
        # the open transaction; __exit__ commits them. No reload/persist needed.
        self._params_flushed = True
        return self

    def add_child_step(
        self,
        child_step: StepSchema,
    ) -> StepSchema:
        """Persist runtime child step after validating process-run ownership."""
        self._ensure_uow()
        if child_step.parent_id is None:
            raise ValueError(
                f"Child step {child_step.name} has no parent_id, was the step created using generate_child()?"
            )
        if child_step.process_run_id != self._process_run.id:
            raise ValueError(
                f"Child step {child_step.name} does not belong to {self._process_run.name}"
            )
        child = self.backend.add_child_step(self.process_run, child_step)
        # refresh cached steps so subsequent operations see the new child
        self._steps = None
        return child

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

    def _ensure_uow(self):
        if self._uow is None:
            self._uow = self.backend.begin()
        return self._uow

    def _ensure_command_saved(self):
        if not self._submitted:
            self.save()

    def _restart_uow(self):
        if self._uow:
            self._uow.rollback()
        self._uow = self.backend.begin()
        self._loaded_in_uow = False
        self._model_dirty = False
        self._params_flushed = False
        return self._uow

    def _reload_process_run(self, process_run_id: UUID) -> ProcessRunSchema:
        runs = self.backend.query(
            ProcessRunSchema,
            QuerySpec(
                filters={"id": process_run_id},
                preloads=["steps", "steps.parameters", "resources"],
                include_mutable=True,
            ),
            namespace_path=self.backend.get_namespace_path(self.namespace_id),
        )
        if not runs:
            raise ValueError(f"ProcessRun with id {process_run_id} not found")
        return runs[0]
