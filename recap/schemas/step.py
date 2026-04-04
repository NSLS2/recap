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

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from recap.db.step import Parameter
from recap.schemas.attribute import (
    AttributeGroupTemplateSchema,
    AttributeTemplateValidator,
    AttributeValueSchema,
)
from recap.schemas.common import CommonFields, StepStatus
from recap.schemas.resource import ResourceSchema, ResourceSlotSchema
from recap.utils.dsl import AliasMixinBase, build_param_values_model


def _attr_metadata(vt: Any) -> dict | None:
    """Extract the metadata dict from an attribute template ORM object or schema.

    Handles the two common shapes: a ``metadata`` attribute (dict) on Pydantic
    models and a ``metadata_json`` attribute (dict) on SQLAlchemy ORM objects.
    Returns an empty dict when neither is present.
    """
    meta = getattr(vt, "metadata", None)
    meta_json = getattr(vt, "metadata_json", None)
    if isinstance(meta, dict):
        return meta
    elif isinstance(meta_json, dict):
        return meta_json
    else:
        return {}


class StepTemplateRef(CommonFields):
    """Lightweight reference to a step template, containing only identity fields.

    Used in serialisation contexts where the full
    :class:`StepTemplateSchema` is not needed to avoid embedding repeated
    attribute group data.

    Attributes:
        name: Human-readable step template name.
    """

    name: str


class StepTemplateSchema(CommonFields):
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

    name: str
    attribute_group_templates: list[AttributeGroupTemplateSchema]
    resource_slots: dict[str, ResourceSlotSchema]


class ParameterSchema(CommonFields):
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
    # values: dict[str, AttributeTemplateSchema]
    values: BaseModel  # dict[str, Any]

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
                av.template.name: {"value": av.value, "unit": av.unit}
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
        runs the value through
        :class:`~recap.schemas.attribute.AttributeTemplateValidator`, and
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

        # 2) coerce each value using your AttributeTemplateValidator
        coerced: dict[str, Any] = {}
        for name, raw_value in values_dict.items():
            attr_tmpl = tmpl_by_name[name]
            if isinstance(raw_value, dict):
                raw_unit = raw_value.get("unit")
                raw_value = raw_value.get("value")
            else:
                raw_unit = None

            # Reuse your validator to perform type coercion & checks
            # Note: we shove `raw_value` into 'default' to leverage coerce_default()
            validator = AttributeTemplateValidator(
                name=attr_tmpl.name,
                type=attr_tmpl.value_type,
                unit=attr_tmpl.unit,
                metadata=_attr_metadata(attr_tmpl),
                default=raw_value,
            )
            coerced[name] = {
                "value": validator.default,  # already converted by coerce_default
                "unit": attr_tmpl.unit if raw_unit is None else raw_unit,
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


class StepSchema(CommonFields):
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

    name: str
    template: StepTemplateSchema
    parameters: BaseModel | dict[str, ParameterSchema]
    state: StepStatus
    process_run_id: UUID
    parent_id: UUID | None = None
    children: list["StepSchema"] = Field(default_factory=list)
    resources: dict[str, "ResourceSchema"] = Field(default_factory=dict)

    def generate_child(self):
        return self.model_copy(deep=True, update={"id": None, "parent_id": self.id})

    @model_validator(mode="after")
    def build_parameter_model(self) -> "StepSchema":
        if isinstance(self.parameters, BaseModel):
            return self

        param_fields: dict[str, Any] = {}
        param_values: dict[str, ParameterSchema] = {}
        for param in self.parameters.values():
            tmpl = param.template
            field_name = getattr(tmpl, "slug", None) or tmpl.name
            param_fields[field_name] = (ParameterSchema, Field(alias=tmpl.name))
            param_values[field_name] = param

        if param_fields:
            model = create_model(
                f"StepParameters_{self.template.name}",
                __base__=AliasMixinBase,
                __config__=ConfigDict(
                    validate_assignment=True,
                    populate_by_name=True,
                    arbitrary_types_allowed=True,
                ),
                **param_fields,
            )
            self.parameters = model.model_validate(param_values)

        return self


StepSchema.model_rebuild()
