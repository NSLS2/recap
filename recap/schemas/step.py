"""Pydantic schemas for process steps, step templates, and step parameters.

This module defines the data models that represent the individual steps inside
a :class:`~recap.schemas.process.ProcessRunSchema`:

* :class:`StepTemplateSchema` — the blueprint for a single workflow step,
  declaring its parameter groups and resource slot roles.
* :class:`ParameterSchema` — a parameter group instance attached to a step,
  combining the template definition with stored
  :class:`~recap.schemas.attribute.AttributeValueSchema` values.
* :class:`StepSchema` — a concrete step instance within a process run,
  carrying live parameter values and a lifecycle state.
* :class:`StepTemplateRef` — a lightweight reference used to avoid circular
  serialisation.
"""

from copy import deepcopy
from datetime import UTC, datetime
from typing import Annotated, Any, ClassVar, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

from recap.db.step import Parameter
from recap.schemas.attribute import (
    AttributeChangeMixin,
    AttributeGroupTemplateSchema,
    AttributeTemplateSchema,
    AttributeValueSchema,
)
from recap.schemas.common import SIMPLE_FIELD, CommonFields, LoadAwareMixin, StepStatus
from recap.schemas.resource import ResourceSchema, ResourceSlotSchema
from recap.utils.dsl import (
    build_param_values_model,
    build_step_parameters_model,
    read_only_unwrap,
)
from recap.utils.general import from_json_value, validate_enum_value


def _attr_metadata(vt: Any) -> dict | None:
    """Extract the metadata dict from an attribute template ORM object or schema.

    Handles the two common shapes: a ``metadata`` attribute (dict) on Pydantic
    models and a ``metadata_json`` attribute (dict) on SQLAlchemy ORM objects.
    Unwraps read-only views before checking the metadata shape.
    Returns an empty dict when neither is present.
    """
    meta = read_only_unwrap(getattr(vt, "metadata", None))
    meta_json = read_only_unwrap(getattr(vt, "metadata_json", None))
    if isinstance(meta, dict):
        return meta
    elif isinstance(meta_json, dict):
        return meta_json
    else:
        return {}


class StepTemplateSchema(LoadAwareMixin, CommonFields):
    """Blueprint for a single workflow step within a process template.

    Declares the parameter groups (attribute group templates) that capture
    data recorded during execution of the step, and the resource slot roles
    that map resources to their function within this particular step.

    Attributes:
        name: Human-readable step name (e.g. ``"Echo Transfer"``).
        attribute_group_templates: Parameter group blueprints whose
            :class:`ParameterSchema` instances will be created when a
            :class:`StepSchema` is instantiated.
        resource_slots: Mapping of role name →
            :class:`~recap.schemas.resource.ResourceSlotSchema` describing
            which resources play which role in this step (e.g. ``"source"``
            and ``"destination"``).
    """

    name: Annotated[str, SIMPLE_FIELD]
    attribute_group_templates: list[AttributeGroupTemplateSchema] = []
    resource_slots: dict[str, ResourceSlotSchema] = {}
    _draft_shared_fields: ClassVar[frozenset[str]] = frozenset({"resource_slots"})
    _relation_fields = frozenset({"attribute_group_templates", "resource_slots"})

    def set_loaded_relations(self, loaded_relations, *, on_unloaded="warn"):
        LoadAwareMixin.set_loaded_relations(
            self, loaded_relations, on_unloaded=on_unloaded
        )
        return self


class ParameterSchema(AttributeChangeMixin, CommonFields):
    """A parameter group instance attached to a step.

    Mirrors the structure of :class:`~recap.schemas.resource.PropertySchema`
    but scoped to a process step rather than a resource.  Parameter values
    record what settings were used when executing a step (e.g. transfer
    volumes, imaging exposure times).

    Parameter values are accessed via the ``step.parameters.<group_slug>``
    shortcut::

        step.parameters.transfer.volume.value   # 100
        step.parameters.transfer.volume = 50    # mutate in-place

    Attributes:
        template: The :class:`~recap.schemas.attribute.AttributeGroupTemplateSchema`
            that defines the parameters in this group.
        values: A dynamically-generated Pydantic model whose fields are
            the attribute slugs, each holding an
            :class:`~recap.schemas.attribute.AttributeValueSchema`.
    """

    template: AttributeGroupTemplateSchema
    _draft_shared_fields: ClassVar[frozenset[str]] = frozenset({"template"})
    values: BaseModel = Field(repr=False)
    _changed_attributes: dict[str, set[str]] = PrivateAttr(default_factory=dict)

    @model_validator(mode="before")
    def coerce_from_orm(cls, data):
        """Translate a SQLAlchemy ``Parameter`` ORM object into the expected dict shape.

        Reads ``template`` and ``_values`` from the ORM object and builds
        the dynamic ``values`` model keyed by attribute name.
        """
        if isinstance(data, Parameter):
            tmpl = data.template
            tmpl_key = tuple(
                (
                    vt.name,
                    vt.slug,
                    vt.value_type,
                    _attr_metadata(vt),
                    vt.unit,
                )
                for vt in tmpl.attribute_templates
            )
            values_model = build_param_values_model(tmpl.slug or tmpl.name, tmpl_key)
            raw_values = {
                av.template.name: {
                    "value": av.value,
                    "unit": av.unit,
                    "metadata_json": av.metadata_json,
                }
                for av in data._values.values()
            }
            return {
                "id": data.id,
                "create_date": data.create_date,
                "modified_date": data.modified_date,
                "template": tmpl,
                "values": values_model.model_validate(raw_values),
            }
        if isinstance(data, dict) and isinstance(data.get("values"), dict):
            tmpl = data.get("template")
            if tmpl:
                tmpl_key = tuple(
                    (
                        vt.name,
                        vt.slug,
                        vt.value_type,
                        _attr_metadata(vt),
                        vt.unit,
                    )
                    for vt in tmpl.attribute_templates
                )
                values_model = build_param_values_model(
                    tmpl.slug or tmpl.name, tmpl_key
                )
                data["values"] = values_model.model_validate(data["values"])
        return data

    @model_validator(mode="before")
    def coerce_and_reject_unknown(cls, data):
        """Validate that all keys in a raw ``values`` dict are known template attributes.

        When *data* is a dict with both ``template`` and ``values`` set,
        checks that every key in ``values`` matches an attribute name in the
        template.  Raises :class:`ValueError` for unrecognised keys and
        constructs the dynamic values model for recognised ones.
        """
        if not isinstance(data, dict):
            return data
        template = data.get("template")
        raw_values = data.get("values") or {}
        if template and isinstance(raw_values, dict):
            tmpl_names = {a.name for a in template.attribute_templates}
            unknown = set(raw_values) - tmpl_names
            if unknown:
                raise ValueError(
                    f"Unknown parameter(s) for template {template.name}: "
                    f"{', '.join(sorted(unknown))}"
                )
            tmpl_key = tuple(
                (
                    vt.name,
                    vt.slug,
                    vt.value_type,
                    _attr_metadata(vt),
                    vt.unit,
                )
                for vt in template.attribute_templates
            )
            values_model = build_param_values_model(
                template.slug or template.name, tmpl_key
            )
            data["values"] = values_model.model_validate(raw_values)
        return data

    @model_validator(mode="after")
    def validate_and_coerce_values(self) -> "ParameterSchema":
        """Type-coerce all stored parameter values against the attribute templates.

        Iterates over every attribute in ``template.attribute_templates``,
        runs the value through shared null-aware Python conversion, and
        rebuilds ``values`` with the coerced results.  Raises
        :class:`ValueError` if any unknown keys are present.
        """
        # Build template lookup: attr name -> template schema
        tmpl_by_name = {a.name: a for a in self.template.attribute_templates}

        values_dict = (
            self.values.model_dump(by_alias=True)
            if isinstance(self.values, BaseModel)
            else dict(self.values)
        )

        # 1) no unknown keys
        unknown_keys = set(values_dict) - set(tmpl_by_name)
        if unknown_keys:
            raise ValueError(
                f"Unknown parameter(s) for template {self.template.name}: "
                f"{', '.join(sorted(unknown_keys))}"
            )

        coerced: dict[str, Any] = {}
        for name, raw_value in values_dict.items():
            attr_tmpl = tmpl_by_name[name]
            if isinstance(raw_value, dict):
                raw_unit = raw_value.get("unit", attr_tmpl.unit)
                raw_metadata = raw_value.get("metadata_json", {})
                raw_value = raw_value.get("value")
            else:
                raw_unit = attr_tmpl.unit
                raw_metadata = getattr(raw_value, "metadata_json", {})

            value = from_json_value(attr_tmpl.value_type, raw_value)
            if attr_tmpl.value_type == "enum":
                validate_enum_value(value, _attr_metadata(attr_tmpl), name=name)
            coerced[name] = {
                "value": value,
                "unit": raw_unit,
                "metadata_json": raw_metadata,
            }

        self.values = self.values.__class__.model_validate(coerced)
        return self

    def __getattr__(self, name: str) -> Any:
        """Proxy attribute access to ``values`` for convenience.

        Called only when normal attribute resolution fails.  Allows
        ``param.batch`` as a shortcut for ``param.values.batch``.

        Raises:
            AttributeError: If *name* is neither a field on this model nor
                an attribute of ``values``.
        """
        # Only called when normal attribute resolution fails.
        # Allows `param.batch` as a shortcut for `param.values.batch`.
        if name.startswith("_"):
            return super().__getattr__(name)
        values = self.__dict__.get("values")
        if values is not None:
            try:
                return getattr(values, name)
            except AttributeError:
                pass
        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )

    def __setattr__(self, name: str, value: Any) -> None:
        """Proxy attribute assignment to ``values``, preserving units.

        Pydantic internals and real model fields are handled normally.
        Unknown names are forwarded to ``values``, mutating ``.value``
        in-place on the :class:`~recap.schemas.attribute.AttributeValueSchema`
        so that the stored unit is preserved.
        """
        # Pydantic internals and real model fields are handled normally.
        # Unknown names are forwarded to values, mutating .value in-place
        # on the AttributeValueSchema so that the unit is preserved.
        if name.startswith("__") or name in type(self).model_fields:
            super().__setattr__(name, value)
            return
        values = self.__dict__.get("values")
        if values is not None and name in type(values).model_fields:
            attr_schema = getattr(values, name)
            if isinstance(attr_schema, AttributeValueSchema):
                attr_schema.value = value
                return
            setattr(values, name, value)
            return
        super().__setattr__(name, value)

    def __getitem__(self, name: str) -> Any:
        """Bracket read: ``param["attr_name"]`` — accepts slug or original name."""
        values = self.__dict__.get("values")
        if values is not None:
            try:
                return values[name]
            except KeyError:
                pass
        raise KeyError(name)

    def __setitem__(self, name: str, value: Any) -> None:
        """Bracket write: ``param["attr_name"] = v`` — unit-preserving, accepts slug or original name."""
        values = self.__dict__.get("values")
        if values is not None:
            values[name] = value
            return
        raise KeyError(name)

    def get(self, name: str, default: Any = None) -> Any:
        """Dict-style get: ``param.get("attr_name")`` — accepts slug or original name."""
        try:
            return self[name]
        except KeyError:
            return default

    def items(self):
        """Yield ``(original_name, AttributeValueSchema)`` pairs for all attributes."""
        values = self.__dict__.get("values")
        if values is None:
            return
        for field_name, field_info in type(values).model_fields.items():
            original_name = field_info.alias or field_name
            yield original_name, getattr(values, field_name)

    def __repr__(self) -> str:
        attrs = ", ".join(f"{name}={str(av)}" for name, av in self.items())
        return f"ParameterSchema(template={self.template.name!r}, {attrs})"


class StepSchema(LoadAwareMixin, CommonFields):
    """A concrete step instance within a :class:`~recap.schemas.process.ProcessRunSchema`.

    Each step corresponds to one :class:`StepTemplateSchema` from the parent
    process template and carries the actual parameter values recorded during
    execution, plus a lifecycle state.

    Attributes:
        name: Step name, matching the template step name.
        template: The :class:`StepTemplateSchema` this step was created from.
        parameters: A dynamically-generated Pydantic model whose fields are
            the parameter group slugs, each holding a
            :class:`ParameterSchema` instance with live values.
        state: Current lifecycle state; one of
            :attr:`~recap.schemas.common.StepStatus.PENDING`,
            :attr:`~recap.schemas.common.StepStatus.IN_PROGRESS`, or
            :attr:`~recap.schemas.common.StepStatus.COMPLETE`.
        process_run_id: UUID of the parent
            :class:`~recap.schemas.process.ProcessRunSchema`.
    """

    name: Annotated[str, SIMPLE_FIELD]
    template: StepTemplateSchema | None = None
    parameters: BaseModel | dict[str, ParameterSchema] = {}
    state: Annotated[StepStatus, SIMPLE_FIELD] = StepStatus.PENDING
    process_run_id: Annotated[UUID | None, SIMPLE_FIELD] = None
    parent_id: Annotated[UUID | None, SIMPLE_FIELD] = None
    children: dict[str, "StepSchema"] = Field(default_factory=dict)
    resources: dict[str, "ResourceSchema"] = Field(default_factory=dict)
    resource_overrides: dict[str, UUID] = Field(default_factory=dict)
    _parameters_dirty: bool = PrivateAttr(default=False)
    _draft_shared_fields: ClassVar[frozenset[str]] = frozenset(
        {"template", "resources"}
    )

    @property
    def parameters_dirty(self) -> bool:
        return self._parameters_dirty

    _relation_fields = frozenset(
        {"template", "parameters", "children", "resources", "resource_overrides"}
    )

    def set_loaded_relations(self, loaded_relations, *, on_unloaded="warn"):
        LoadAwareMixin.set_loaded_relations(
            self, loaded_relations, on_unloaded=on_unloaded
        )
        return self

    def __getitem__(self, name: str) -> "StepSchema":
        return self.children[name]

    @field_validator("children", mode="before")
    @classmethod
    def normalize_children(cls, data):
        if isinstance(data, list):
            return {
                child.name if hasattr(child, "name") else child["name"]: child
                for child in data
            }
        return data

    @staticmethod
    def _template_parameter_value(attribute: AttributeTemplateSchema) -> dict[str, Any]:
        return {
            "value": from_json_value(
                attribute.value_type,
                deepcopy(read_only_unwrap(attribute.default_value)),
            ),
            "unit": attribute.unit,
            "metadata_json": {},
        }

    def _parent_parameter_value(
        self, parent_param: ParameterSchema | None, attribute: AttributeTemplateSchema
    ) -> dict[str, Any]:
        value = parent_param.get(attribute.name) if parent_param is not None else None
        if value is None:
            return self._template_parameter_value(attribute)
        return {
            "value": deepcopy(read_only_unwrap(value.value)),
            "unit": value.unit,
            "metadata_json": deepcopy(read_only_unwrap(value.metadata_json)),
        }

    def generate_child(
        self, *, name: str, inherit: Literal["template", "parent"] = "template"
    ) -> "StepSchema":
        """Create a pending child with independent parameter values.

        ``inherit="template"`` (the default) uses template defaults.
        ``inherit="parent"`` snapshots the parent's current values, units,
        and value metadata, including unsaved edits. Missing parent parameters
        fall back to template defaults. Resource inheritance is unchanged.

        Raises:
            ValueError: If the mode, parent, or name is invalid, template
                definitions are unloaded, or parent-mode parameters are unloaded.
        """
        if inherit not in ("template", "parent"):
            raise ValueError(f"inherit must be 'template' or 'parent', got {inherit!r}")
        if self.parent_id is not None:
            raise ValueError("Children require a template-generated parent step")
        if not name.strip():
            raise ValueError("Child step name must not be empty")
        template = self.template
        if template is None:
            raise ValueError(
                "Child generation requires loaded step template definitions"
            )
        if (
            template.__pydantic_private__.get("_loaded_relations", {}).get(
                "attribute_group_templates"
            )
            is False
        ):
            raise ValueError(
                "Child generation requires loaded step template definitions"
            )
        parent_params = {}
        if inherit == "parent":
            if self._loaded_relations.get("parameters") is False:
                raise ValueError(
                    "Child generation with inherit='parent' requires loaded parent parameters"
                )
            source = self.parameters
            groups = (
                (getattr(source, field) for field in type(source).model_fields)
                if isinstance(source, BaseModel)
                else source.values()
            )
            parent_params = {param.template.name: param for param in groups}

        now = datetime.now(UTC)
        parameters = {}
        for group in template.attribute_group_templates:
            values = {
                attribute.name: (
                    self._parent_parameter_value(
                        parent_params.get(group.name), attribute
                    )
                    if inherit == "parent"
                    else self._template_parameter_value(attribute)
                )
                for attribute in group.attribute_templates
            }
            parameters[group.name] = ParameterSchema(
                id=uuid4(),
                create_date=now,
                modified_date=now,
                template=group,
                values=values,
            )
        return StepSchema(
            id=uuid4(),
            create_date=now,
            modified_date=now,
            name=name,
            template=template,
            parameters=parameters,
            process_run_id=self.process_run_id,
            parent_id=self.id,
            resources=dict(self.resources),
            children={},
            state=StepStatus.PENDING,
        )

    @model_validator(mode="after")
    def build_parameter_model(self) -> "StepSchema":
        if isinstance(self.parameters, BaseModel):
            return self

        param_fields: list[tuple[str, str]] = []
        param_values: dict[str, ParameterSchema] = {}
        for param in self.parameters.values():
            tmpl = param.template
            field_name = getattr(tmpl, "slug", None) or tmpl.name
            param_fields.append((field_name, tmpl.name))
            param_values[field_name] = param

        if param_fields:
            template = self.__dict__.get("template")
            if template is None:
                return self
            model = build_step_parameters_model(
                template.name,
                tuple(param_fields),
            )
            self.parameters = model.model_validate(param_values)

        return self


StepTemplateRef = StepTemplateSchema
StepTemplate = StepTemplateSchema
StepTemplateSchema.model_rebuild(force=True)
StepSchema.model_rebuild(force=True)
