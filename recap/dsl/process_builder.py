import warnings
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, create_model
from sqlalchemy.exc import NoResultFound

from recap.client.backend import ClientBackend
from recap.commands.models import (
    CommandContext,
    CopyProcessRun,
    CreateProcessRun,
    CreateProcessTemplate,
    SetLifecycleStatus,
    UpdateProcessRun,
    UpdateProcessTemplate,
)
from recap.dsl.attribute_builder import AttributeGroupBuilder
from recap.dsl.builder_state import BuilderChanges, BuilderTransactionState
from recap.dsl.drafts import (
    AttributeDraft,
    AttributeGroupDraft,
    ProcessRunDraft,
    ProcessRunStepDraft,
    ProcessTemplateDraft,
    ResourceSlotDraft,
    StepTemplateDraft,
    detached_model,
)
from recap.dsl.query import QuerySpec
from recap.exceptions import (
    ExistingProcessRunError,
    ExistingProcessRunWarning,
    ExistingProcessTemplateError,
    ExistingProcessTemplateWarning,
    RecapNotFoundError,
)
from recap.lifecycle import LifecycleStatus
from recap.schemas.attribute import AttributeTemplateValidator
from recap.schemas.namespace import NamespaceContext
from recap.schemas.process import (
    ProcessRunSchema,
    ProcessTemplateRef,
    ProcessTemplateSchema,
)
from recap.schemas.resource import ResourceAssignmentSchema, ResourceSchema
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
        name: str | None,
        version: str | None,
        *,
        backend: ClientBackend,
        namespace_context: NamespaceContext,
        command_context: CommandContext,
        process_template_id: UUID | None = None,
        on_existing: Literal["silent", "warn", "raise"] = "warn",
    ):
        self.backend = backend
        self.namespace_context = namespace_context
        self._command_context = command_context
        self._submitted = False
        self._last_draft = None
        self._transaction = BuilderTransactionState()
        self._expected_revision = 1
        self._draft_labels: list[str] = []
        self._draft_resource_slots: list[ResourceSlotDraft] = []
        self._draft_steps: list[StepTemplateBuilder] = []
        if on_existing not in {"silent", "warn", "raise"}:
            raise ValueError("on_existing must be one of: 'silent', 'warn', 'raise'")
        self.on_existing = on_existing
        self.name = name
        self.version = version
        self._template: ProcessTemplateRef | None = None
        self._is_new_template = process_template_id is None
        self._draft_model: ProcessTemplateSchema | None = None
        self._resource_slots = {}
        self._current_step_builder = None
        if process_template_id is not None:
            self._initialize_command_update(process_template_id)
        elif name is None or version is None:
            raise ValueError(
                "name and version are required to create a process template"
            )
        else:
            self._template = ProcessTemplateRef.model_construct(
                id=uuid4(),
                create_date=None,
                modified_date=None,
                namespace_id=self.namespace_context.id,
                status=LifecycleStatus.MUTABLE,
                revision=1,
                name=name,
                version=version,
                labels=[],
            )
            existing = self.backend.query(
                ProcessTemplateSchema,
                QuerySpec(
                    filters={"name": name, "version": version},
                    preloads=["step_templates", "resource_slots"],
                    include_mutable=True,
                ),
                namespace_path=self.namespace_context.path,
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
        self._transaction.enter()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._transaction.exit(exc_type):
            self._flush()

    def save(self):
        if not self._transaction.in_context:
            raise RuntimeError("Builder changes require a context manager")
        return self._flush()

    def changes(self) -> BuilderChanges:
        draft = self._build_draft()
        return BuilderChanges(
            fields={"draft": draft.model_dump(mode="json")}
            if draft != self._last_draft
            else {},
            lifecycle=self._transaction.pending_lifecycle,
        )

    def _flush(self):
        draft = self._build_draft()
        if draft != self._last_draft:
            command = (
                CreateProcessTemplate(
                    namespace_path=self.namespace_context.path, draft=draft
                )
                if self._is_new_template
                else UpdateProcessTemplate(
                    template_id=self._template.id,
                    expected_revision=self._expected_revision,
                    draft=draft,
                )
            )
            result = self.backend._execute(command, self._command_context)
            if result is None:
                return self
            self._template = result
            self._expected_revision = result.revision
            self._last_draft = draft
            self._submitted = True
        elif self._template is not None:
            self._last_draft = draft

        pending = self._transaction.pending_lifecycle
        if pending is None:
            return self
        self._ensure_template()
        result = self.backend._execute(
            SetLifecycleStatus(
                object_type="process_template",
                object_id=self.template.id,
                expected_revision=self._template.revision,
                status=pending.value,
            ),
            self._command_context,
        )
        if isinstance(result, ProcessTemplateSchema):
            self._template = result
            self._expected_revision = result.revision
            self._transaction.clear_lifecycle()
        return self

    def finalize(self):
        self._transaction.request_lifecycle(LifecycleStatus.ACTIVE)
        return self

    def archive(self):
        self._transaction.request_lifecycle(LifecycleStatus.ARCHIVED)
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
            if (
                existing.resource_type != resource_type
                or existing.direction != direction
            ):
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
        if (update and self._transaction.in_context) or (
            self._template is None and self._transaction.in_context
        ):
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
        self._draft_model = detached_model(model)
        if isinstance(model, ProcessTemplateSchema):
            self.name = model.name
            self.version = model.version
            self._draft_labels = list(model.labels)
        self._submitted = False

    def _build_draft(self) -> ProcessTemplateDraft:
        if self._draft_model is not None:
            model = self._draft_model
            return ProcessTemplateDraft(
                id=model.id,
                name=model.name,
                version=model.version,
                labels=model.labels,
                resource_slots=[
                    ResourceSlotDraft(
                        name=slot.name,
                        resource_type=slot.resource_type.name,
                        direction=slot.direction,
                        required=slot.required,
                    )
                    for slot in model.resource_slots
                ],
                steps=[
                    StepTemplateDraft(
                        name=step.name,
                        role_bindings={
                            role: slot.name
                            for role, slot in step.resource_slots.items()
                        },
                        parameter_groups=[
                            AttributeGroupDraft(
                                name=group.name,
                                attributes=[
                                    AttributeDraft(
                                        name=attribute.name,
                                        type=attribute.value_type,
                                        unit=attribute.unit or "",
                                        default=attribute.default_value,
                                        metadata=attribute.metadata or {},
                                    )
                                    for attribute in group.attribute_templates
                                ],
                            )
                            for group in step.attribute_group_templates
                        ],
                    )
                    for step in model.step_templates.values()
                ],
            )
        return ProcessTemplateDraft(
            id=self._template.id,
            name=self.name,
            version=self.version,
            labels=self._draft_labels,
            resource_slots=self._draft_resource_slots,
            steps=[step._build_draft() for step in self._draft_steps],
        )

    def _initialize_command_update(self, process_template_id: UUID) -> None:
        self._is_new_template = False
        templates = self.backend.query(
            ProcessTemplateSchema,
            QuerySpec(
                filters={"id": process_template_id},
                preloads=["step_templates", "resource_slots"],
                include_mutable=True,
            ),
            namespace_path=self.namespace_context.path,
        )
        if not templates:
            raise RecapNotFoundError(
                f"ProcessTemplate with id {process_template_id} not found"
            )
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
        self._last_draft = self._build_draft()
        self._submitted = True


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
        version: str | None = None,
        *,
        backend: ClientBackend,
        namespace_context: NamespaceContext,
        process_run_id: UUID | None = None,
        on_existing: Literal["silent", "warn", "raise"] = "warn",
        command_context: CommandContext,
        template_id: UUID | None = None,
    ):
        if command_context is None:
            raise ValueError("ProcessRunBuilder requires command context")
        self.backend = backend
        self.name = name
        self.description = description
        self.namespace_context = namespace_context
        self.template_name = template_name
        self.version = version
        if on_existing not in {"silent", "warn", "raise"}:
            raise ValueError("on_existing must be one of: 'silent', 'warn', 'raise'")
        self.on_existing = on_existing
        self._command_context = command_context
        self._template_id = template_id
        self._process_template = None
        self._submitted = False
        self._save_called = False
        self._transaction = BuilderTransactionState()
        self._draft_assignments: dict[str, UUID] = {}
        self._draft_steps: dict[str, dict[str, dict[str, object]]] = {}
        if (
            template_id is None
            and process_run_id is None
            and (template_name is None or version is None)
        ):
            raise ValueError(
                "template_id or process_run_id are required for command-backed builders"
            )
        self._process_run = (
            self._reload_process_run(process_run_id)
            if process_run_id is not None
            else None
        )
        self._draft_process_run = (
            self._process_run.model_copy(deep=True)
            if self._process_run is not None
            else None
        )
        self._baseline_process_run = (
            self._process_run.model_copy(deep=True)
            if self._process_run is not None
            else None
        )
        self._dirty = False
        if self._process_run is not None:
            self.name = self._process_run.name
            self.description = self._process_run.description
            # self._template_id = self._process_run.__dict__["template"].id
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
            namespace_path=namespace_context.path,
        )
        if not templates:
            raise ValueError(
                f"Process template {self.template_name!r} version {version!r} not found"
            )
        self._process_template = templates[0]
        self._template_id = self._process_template.id
        if self._process_run is None:
            self._draft_process_run = ProcessRunSchema.model_construct(
                id=uuid4(),
                create_date=None,
                modified_date=None,
                namespace_id=self.namespace_context.id,
                status=LifecycleStatus.MUTABLE,
                revision=1,
                name=self.name,
                description=self.description,
                template=self._process_template,
                steps={},
                assigned_resources={},
            )
        if self._process_run is None:
            existing_runs = self.backend.query(
                ProcessRunSchema,
                QuerySpec(
                    filters={"name": name},
                    preloads=["template", "steps", "steps.parameters", "resources"],
                    include_mutable=True,
                ),
                namespace_path=namespace_context.path,
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
                if not isinstance(self._process_run, ProcessRunSchema):
                    raise TypeError(
                        "Process-run builder requires ProcessRunSchema result"
                    )
                self._draft_process_run = self._process_run.model_copy(deep=True)
                self.name = self._process_run.name
                self.description = self._process_run.description
        self._steps = (
            list(self._process_run.steps.values())
            if self._process_run is not None
            else []
        )
        if self._process_run is not None:
            self._draft_process_run = self._process_run.model_copy(deep=True)
            self._submitted = True
        self._baseline_process_run = (
            self._process_run.model_copy(deep=True)
            if self._process_run is not None
            else None
        )
        self._draft_assignments = self._assignment_ids(self._draft_process_run)
        self._draft_steps = self._model_steps_payload(self._draft_process_run)
        self._resources = {}

    def __enter__(self):
        self._transaction.enter()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._transaction.exit(exc_type) and not self._save_called:
            self.save()

    def save(self):
        """Validate and persist current process-run changes."""
        if self._submitted and not self._dirty and self._pending_lifecycle() is None:
            return self
        assignments = self._assignment_changes()
        steps = self._step_changes()
        description = self._description_change()
        needs_data = (
            not self._submitted
            or bool(assignments)
            or bool(steps)
            or description is not None
        )
        result = self._process_run
        if needs_data and self._process_run is None:
            command = CreateProcessRun(
                namespace_path=self.namespace_context.path,
                draft=ProcessRunDraft(
                    id=self._draft_process_run.id,
                    name=self.name,
                    description=self.description,
                    template_id=self._template_id,
                    assignments=assignments,
                    steps={
                        name: ProcessRunStepDraft(parameters=value)
                        for name, value in self._draft_steps.items()
                    },
                ),
            )
        elif needs_data and self._process_run.status is not LifecycleStatus.MUTABLE:
            command = CopyProcessRun(
                source_process_run_id=self._process_run.id,
                destination_namespace_path=self.namespace_context.path,
                options={
                    "changes": {
                        "description": description,
                        "assignments": assignments or None,
                        "steps": steps or None,
                    }
                },
            )
        elif needs_data:
            command = UpdateProcessRun(
                process_run_id=self._process_run.id,
                expected_revision=self._process_run.revision,
                description=description,
                assignments=assignments or None,
                steps=steps or None,
            )
        if needs_data:
            result = self.backend._execute(command, self._command_context)
            self._save_called = True
            if not isinstance(result, ProcessRunSchema):
                return self
            self._process_run = result
            self._draft_process_run = result.model_copy(deep=True)
            self._baseline_process_run = result.model_copy(deep=True)
            self._draft_assignments = self._assignment_ids(result)
            self._draft_steps = self._model_steps_payload(result)
            self.name = result.name
            self.description = result.description
            self._steps = list(result.steps.values())
            self._dirty = False
            self._submitted = True

        pending = self._pending_lifecycle()
        if pending is not None:
            if self._process_run is None:
                return self
            result = self.backend._execute(
                SetLifecycleStatus(
                    object_type="process_run",
                    object_id=self._process_run.id,
                    expected_revision=self._process_run.revision,
                    status=pending.value,
                ),
                self._command_context,
            )
            if isinstance(result, ProcessRunSchema):
                self._process_run = result
                self._draft_process_run = result.model_copy(deep=True)
                self._baseline_process_run = result.model_copy(deep=True)
                self._draft_assignments = self._assignment_ids(result)
                self._draft_steps = self._model_steps_payload(result)
                self._steps = list(result.steps.values())
                self._transaction.clear_lifecycle(owner=self)
        return self

    def finalize(self):
        """Transition process run to ACTIVE/finalized state."""
        self._transaction.request_lifecycle(LifecycleStatus.ACTIVE, owner=self)
        if not self._transaction.in_context:
            self.save()
        return self

    def archive(self):
        """Transition process run to ARCHIVED."""
        self._transaction.request_lifecycle(LifecycleStatus.ARCHIVED, owner=self)
        if not self._transaction.in_context:
            self.save()
        return self

    def changes(self) -> BuilderChanges:
        fields = {}
        assignments = self._assignment_changes()
        steps = self._step_changes()
        description = self._description_change()
        if assignments:
            fields["assignments"] = assignments
        if steps:
            fields["steps"] = steps
        if description is not None:
            fields["description"] = description
        return BuilderChanges(
            fields=fields,
            lifecycle=self._pending_lifecycle(),
        )

    @property
    def process_run(self) -> ProcessRunSchema:
        return self._draft_process_run

    def set_model(self, model: ProcessRunSchema):
        """Replace working run model after verifying UUID identity."""
        if self._process_run is None:
            raise RuntimeError("ProcessRun not initialized")
        if model.id != self._process_run.id:
            raise ValueError("ID for this ProcessRun does not match the builder")
        self._draft_process_run = detached_model(model)
        self.name = model.name
        self.description = model.description
        self._draft_assignments = self._assignment_ids(model)
        self._draft_steps = self._model_steps_payload(model)
        self._draft_process_run = detached_model(model)
        self._dirty = True
        self._save_called = False

    def assign_resource(
        # self, resource_slot_name: str, resource_name: str, resource_template_name: str
        self,
        resource_slot_name: str,
        resource: ResourceSchema,
    ) -> "ProcessRunBuilder":
        """Assign resource to named process slot."""
        self._draft_assignments[resource_slot_name] = resource.id
        slot = next(
            (
                slot
                for slot in self._process_template.resource_slots
                if slot.name == resource_slot_name
            ),
            None,
        )
        if slot is not None:
            self._draft_process_run.assigned_resources[resource_slot_name] = (
                ResourceAssignmentSchema(slot=slot, resource=resource)
            )
        self._dirty = True
        self._save_called = False
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
        if self._process_run is None:
            self.save()
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
                **{
                    name: getattr(parameters, name)
                    for name in type(parameters).model_fields
                },
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
                values[attr_info.alias or attr_name] = {
                    "value": value.value,
                    "unit": value.unit,
                }
            step_data[field_info.alias or field_name] = values
        self._draft_steps[filled_params.step_name] = step_data
        self._dirty = True
        self._save_called = False
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
            result[step.name] = {"parameters": groups}
        return result

    def get_model(self, *, update: bool = False) -> ProcessRunSchema:
        """
        Return a pydantic model representing the process run, optionally reloading
        from the backend first. Critical fields are locked against mutation.
        """
        if update:
            self._process_run = self._reload_process_run(self._process_run.id)
            self._draft_process_run = self._process_run.model_copy(deep=True)
        model = (self._draft_process_run or self._process_run).model_copy(deep=True)
        return lock_instance_fields(
            model, {"id", "create_date", "modified_date", "template"}
        )

    def _ensure_command_saved(self):
        if not self._submitted:
            self.save()

    def _pending_lifecycle(self):
        return self._transaction.pending_lifecycle_for(self)

    @staticmethod
    def _assignment_ids(model: ProcessRunSchema | None) -> dict[str, UUID]:
        if model is None:
            return {}
        return {
            name: assignment.resource.id
            for name, assignment in model.assigned_resources.items()
        }

    def _assignment_changes(self) -> dict[str, UUID]:
        current = self._draft_assignments
        baseline = self._assignment_ids(self._baseline_process_run)
        if self._baseline_process_run is None:
            return dict(current)
        return {
            name: resource_id
            for name, resource_id in current.items()
            if baseline.get(name) != resource_id
        }

    def _step_changes(self):
        if self._baseline_process_run is None:
            return dict(self._draft_steps)
        baseline = self._model_steps_payload(self._baseline_process_run)
        return {
            name: value
            for name, value in self._draft_steps.items()
            if baseline.get(name) != value
        }

    def _description_change(self):
        if (
            self._baseline_process_run is None
            or self.description != self._baseline_process_run.description
        ):
            return self.description
        return None

    def _reload_process_run(self, process_run_id: UUID) -> ProcessRunSchema:
        runs = self.backend.query(
            ProcessRunSchema,
            QuerySpec(
                filters={"id": process_run_id},
                preloads=["template", "steps", "steps.parameters", "resources"],
                include_mutable=True,
            ),
            namespace_path=self.namespace_context.path,
        )
        if not runs:
            raise RecapNotFoundError(f"ProcessRun with id {process_run_id} not found")
        return runs[0]
