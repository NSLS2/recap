"""Pydantic schemas for resources, resource templates, and related structures.

This module defines the data models for the core RECAP resource hierarchy:

* :class:`ResourceTemplateSchema` — the blueprint that describes the
  structure (children, property groups) of a category of resources.
* :class:`ResourceSchema` — a concrete instance of a template, carrying
  live property values and any instantiated children.
* :class:`PropertySchema` — a single property group attached to a resource,
  combining the template definition with the stored :class:`~recap.schemas.attribute.AttributeValueSchema` values.
* Supporting classes: :class:`ResourceTypeSchema`, :class:`ResourceTemplateRef`,
  :class:`ResourceSlotSchema`, :class:`ResourceRef`, :class:`ResourceAssignmentSchema`.
"""

import warnings
from typing import Annotated, Any, Literal

try:
    from typing import Self
except ImportError:  # Python <3.11
    from typing_extensions import Self  # noqa

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from recap.db.resource import Property
from recap.exceptions import UnloadedFieldError, UnloadedFieldWarning
from recap.schemas.attribute import (
    AttributeGroupTemplateSchema,
    AttributeTemplateValidator,
    AttributeValueSchema,
)
from recap.schemas.common import (
    SIMPLE_FIELD,
    CommonFields,
    LoadAwareMixin,
    NamespaceOwnedFields,
    NormalizedLabels,
)
from recap.utils.dsl import build_param_values_model, build_property_groups_model
from recap.utils.general import Direction


def _attr_metadata(vt: Any) -> dict | None:
    """Extract the metadata dict from an attribute template ORM object or schema.

    Handles the two common shapes: a ``metadata`` attribute (dict) returned by
    Pydantic models and a ``metadata_json`` attribute (dict) returned by
    SQLAlchemy ORM objects.  Returns an empty dict when neither is present.
    """
    meta = getattr(vt, "metadata", None)
    meta_json = getattr(vt, "metadata_json", None)
    if isinstance(meta, dict):
        return meta
    elif isinstance(meta_json, dict):
        return meta_json
    else:
        return {}


class PropertySchema(CommonFields):
    """A property group instance attached to a resource.

    Combines the group template (field definitions, types, units) with the
    actual stored values for one named group of attributes.  Property groups
    are accessed via the ``resource.properties.<group_slug>`` shortcut::

        plate.properties.dimensions.rows.value   # 8
        plate.properties.dimensions.rows = 12    # mutate in-place

    The ``values`` field is a dynamically-created Pydantic model whose fields
    correspond to the attribute slugs defined in the group template.  Each
    field is an :class:`~recap.schemas.attribute.AttributeValueSchema`.

    Attributes:
        template: The :class:`~recap.schemas.attribute.AttributeGroupTemplateSchema`
            that defines the attributes in this group.
        values: A dynamically-generated Pydantic model whose fields are the
            attribute slugs, each holding an
            :class:`~recap.schemas.attribute.AttributeValueSchema`.
    """

    template: AttributeGroupTemplateSchema
    values: BaseModel = Field(repr=False)

    @model_validator(mode="before")
    def coerce_from_orm_or_dict(cls, data):
        """Translate an ORM ``Property`` object or a raw ``dict`` into the expected shape.

        *ORM path*: Reads ``template`` and ``_values`` from the SQLAlchemy
        ``Property`` object and builds the dynamic ``values`` model.

        *Dict path*: When ``values`` is a plain ``dict``, validates that all
        keys are known attributes of the template and constructs the dynamic
        ``values`` model.
        """
        if isinstance(data, Property):
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
                tmpl_names = {a.name for a in tmpl.attribute_templates}
                unknown = set(data["values"]) - tmpl_names
                if unknown:
                    raise ValueError(
                        f"Unknown property(s) for template {tmpl.name}: "
                        f"{', '.join(sorted(unknown))}"
                    )
                tmpl_key = tuple(
                    (vt.name, vt.slug, vt.value_type, _attr_metadata(vt), vt.unit)
                    for vt in tmpl.attribute_templates
                )
                values_model = build_param_values_model(
                    tmpl.slug or tmpl.name, tmpl_key
                )
                data["values"] = values_model.model_validate(data["values"])
        return data

    @model_validator(mode="after")
    def validate_and_coerce_values(self) -> "PropertySchema":
        """Type-coerce all stored values against the attribute templates.

        Iterates over every attribute in ``template.attribute_templates``,
        runs the value through
        :class:`~recap.schemas.attribute.AttributeTemplateValidator`, and
        rebuilds ``values`` with the coerced results.  Raises
        :class:`ValueError` if any unknown keys are present.
        """
        tmpl_by_name = {a.name: a for a in self.template.attribute_templates}

        values_dict = (
            self.values.model_dump(by_alias=True)
            if isinstance(self.values, BaseModel)
            else dict(self.values)
        )

        unknown_keys = set(values_dict) - set(tmpl_by_name)
        if unknown_keys:
            raise ValueError(
                f"Unknown property(s) for template {self.template.name}: "
                f"{', '.join(sorted(unknown_keys))}"
            )

        coerced: dict[str, Any] = {}
        for name, raw_value in values_dict.items():
            attr_tmpl = tmpl_by_name[name]
            if isinstance(raw_value, dict):
                raw_unit = raw_value.get("unit")
                raw_metadata = raw_value.get("metadata_json", {})
                raw_value = raw_value.get("value")
            else:
                raw_unit = None
                raw_metadata = {}

            validator = AttributeTemplateValidator(
                name=attr_tmpl.name,
                type=attr_tmpl.value_type,
                unit=attr_tmpl.unit,
                metadata=_attr_metadata(attr_tmpl),
                default=raw_value,
            )
            coerced[name] = {
                "value": raw_value if attr_tmpl.value_type == "array" else validator.default,
                "unit": attr_tmpl.unit if raw_unit is None else raw_unit,
                "metadata_json": raw_metadata,
            }

        self.values = self.values.__class__.model_validate(coerced)
        return self

    def __getattr__(self, name: str) -> Any:
        """Proxy attribute access to ``values`` for convenience.

        Called only when normal attribute resolution fails (i.e. *name* is not
        a declared field such as ``template``, ``values``, or ``id``).  This
        allows ``prop.smiles`` as a shortcut for ``prop.values.smiles``.

        Raises:
            AttributeError: If *name* is neither a field on this model nor
                an attribute of ``values``.
        """
        # Only called when normal attribute resolution fails (i.e. `name` is not
        # a real field on PropertySchema such as `template`, `values`, `id`, etc.)
        # This allows `prop.smiles` as a shortcut for `prop.values.smiles`.
        # Note: if an attribute group defines a field that clashes with a
        # PropertySchema field name, the shortcut won't reach it.
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
        """Bracket read: ``prop["attr_name"]`` — accepts slug or original name."""
        values = self.__dict__.get("values")
        if values is not None:
            try:
                return values[name]
            except KeyError:
                pass
        raise KeyError(name)

    def __setitem__(self, name: str, value: Any) -> None:
        """Bracket write: ``prop["attr_name"] = v`` — unit-preserving, accepts slug or original name."""
        values = self.__dict__.get("values")
        if values is not None:
            values[name] = value
            return
        raise KeyError(name)

    def get(self, name: str, default: Any = None) -> Any:
        """Dict-style get: ``prop.get("attr_name")`` — accepts slug or original name."""
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
        return f"PropertySchema(template={self.template.name!r}, {attrs})"


class ResourceTypeSchema(CommonFields):
    """A type tag associated with a resource or resource template.

    Resources can carry multiple type tags (e.g. ``"container"``,
    ``"plate"``, ``"library_plate"``).  Process template resource slots
    filter compatible resources by matching against these tags.

    Attributes:
        name: The type tag string (e.g. ``"library_plate"``).
    """

    name: Annotated[str, SIMPLE_FIELD]


class ResourceCopyChanges(BaseModel):
    """Property changes applied while copying a resource."""

    properties: dict[str, dict[str, Any]] = Field(default_factory=dict)


class ResourceCopyOptions(BaseModel):
    """Name and property overrides for a resource copy operation."""

    name: str | None = None
    changes: ResourceCopyChanges = Field(default_factory=ResourceCopyChanges)


class ResourceTemplateRef(NamespaceOwnedFields):
    """Lightweight reference to a resource template, without child or property detail.

    Used when a full :class:`ResourceTemplateSchema` is not required, e.g.
    inside a :class:`ResourceSchema` to avoid infinite recursion when
    serialising deeply nested resource trees.

    Attributes:
        name: Template name.
        slug: Snake_case identifier.
        version: Version string (e.g. ``"1.0"``).
        parent: Reference to the parent template, or ``None`` for top-level
            templates.  Excluded from serialisation.
        types: List of type tags associated with this template.
    """

    name: Annotated[str, SIMPLE_FIELD]
    slug: Annotated[str | None, SIMPLE_FIELD]
    version: Annotated[str, SIMPLE_FIELD]
    labels: NormalizedLabels
    parent: Self | None = Field(default=None, exclude=True)
    types: list[ResourceTypeSchema] = Field(default_factory=list)


class ResourceTemplateSchema(LoadAwareMixin, NamespaceOwnedFields):
    """Full blueprint for a category of resources.

    Defines the complete structure of a resource: its type tags, optional
    parent template, child templates (and their own property groups), and
    the attribute group templates whose property groups will be instantiated
    on every resource created from this template.

    Attributes:
        name: Unique human-readable template name.
        slug: Snake_case identifier derived from *name*.
        version: Version string (e.g. ``"1.0"``).
        types: List of :class:`ResourceTypeSchema` tags.
        parent: Reference to the parent template, excluded from serialisation.
        children: Mapping of child name → child :class:`ResourceTemplateSchema`
            for nested resource hierarchies (e.g. wells inside a plate).
        attribute_group_templates: Property group blueprints that will be
            instantiated on each :class:`ResourceSchema`.
    """

    name: Annotated[str, SIMPLE_FIELD]
    slug: Annotated[str | None, SIMPLE_FIELD] = None
    version: Annotated[str, SIMPLE_FIELD]
    labels: NormalizedLabels = Field(default_factory=list)
    types: list[ResourceTypeSchema] = Field(default_factory=list)
    parent: ResourceTemplateRef | None = Field(default=None, exclude=True)
    children: dict[str, Self] = Field(default_factory=dict)
    attribute_group_templates: list[AttributeGroupTemplateSchema] = Field(default_factory=list)
    _relation_fields = frozenset({"parent", "children", "types", "attribute_group_templates"})

    def set_loaded_relations(self, loaded_relations, *, on_unloaded="warn"):
        LoadAwareMixin.set_loaded_relations(self, loaded_relations, on_unloaded=on_unloaded)
        return self

    @field_validator("parent", mode="before")
    @classmethod
    def _coerce_parent_reference(cls, value):
        if value is None or isinstance(value, dict):
            return value
        return {
            "id": value.id,
            "create_date": value.create_date,
            "modified_date": value.modified_date,
            "namespace_id": value.namespace_id,
            "status": value.status,
            "revision": value.revision,
            "name": value.name,
            "slug": value.slug,
            "version": value.version,
            "labels": value.labels,
            "types": list(value.types),
        }


ResourceTypeSchema.model_rebuild()


class ResourceSlotSchema(CommonFields):
    """A typed slot on a process template that accepts a specific resource type.

    Resource slots declare which resources can be wired into a
    :class:`~recap.schemas.process.ProcessTemplateSchema` and in which
    direction they flow.

    Attributes:
        name: Slot name as declared on the process template.
        resource_type: The :class:`ResourceTypeSchema` tag that a resource
            must carry to be assigned to this slot.
        direction: :class:`~recap.utils.general.Direction` — typically
            ``INPUT`` or ``OUTPUT``.
    """

    name: Annotated[str, SIMPLE_FIELD]
    resource_type: ResourceTypeSchema
    direction: Annotated[Direction, SIMPLE_FIELD]
    required: Annotated[bool, SIMPLE_FIELD] = True


class ResourceSchema(LoadAwareMixin, NamespaceOwnedFields):
    """A concrete resource instance created from a :class:`ResourceTemplateSchema`.

    Resources are the primary trackable entities in RECAP.  They can represent
    physical objects (plates, samples), digital artifacts (data files), or
    logical items (computed results).  Every resource carries:

    * An identity (:attr:`~recap.schemas.common.CommonFields.id`,
      :attr:`name`).
    * A reference to the template it was created from.
    * A hierarchy of child resources (auto-created from the template).
    * Property groups populated with live :class:`~recap.schemas.attribute.AttributeValueSchema`
      values.

    Accessing and updating properties::

        well = plate.children["A01"]
        well.properties.content.volume.value   # 10.0
        well.properties.content.volume = 8.5   # mutates in-place

    Attributes:
        name: Display name of this resource instance.
        template: The :class:`ResourceTemplateSchema` this instance was
            created from.
        parent: Lightweight reference to the parent resource, or ``None``
            for top-level resources.  Excluded from serialisation.
        children: Mapping of child name → child :class:`ResourceSchema`.
        properties: A dynamically-generated Pydantic model whose fields are
            the property group slugs, each holding a
            :class:`PropertySchema`.
    """

    name: Annotated[str, SIMPLE_FIELD]
    copied_from_id: Annotated[UUID | None, SIMPLE_FIELD] = None
    template: ResourceTemplateSchema | None = None
    parent: "ResourceSchema | None" = Field(default=None, exclude=True)
    children: dict[str, Self] = Field(default_factory=dict)
    properties: BaseModel | dict[str, PropertySchema] = Field(default_factory=dict)
    model_config = ConfigDict(arbitrary_types_allowed=True, from_attributes=True)
    _relation_fields = frozenset({"template", "parent", "children", "properties"})

    @field_validator("parent", mode="before")
    @classmethod
    def _coerce_parent_reference(cls, value):
        if value is None or isinstance(value, (dict, ResourceSchema)):
            return value
        return {
            "id": value.id,
            "create_date": value.create_date,
            "modified_date": value.modified_date,
            "namespace_id": value.namespace_id,
            "status": value.status,
            "revision": value.revision,
            "name": value.name,
            "copied_from_id": value.copied_from_id,
            "template": value.template,
        }

    @field_validator("template", mode="before")
    @classmethod
    def _coerce_template_reference(cls, value):
        if value is None or isinstance(value, (dict, ResourceTemplateSchema)):
            return value
        return {
            "id": value.id,
            "create_date": value.create_date,
            "modified_date": value.modified_date,
            "namespace_id": value.namespace_id,
            "status": value.status,
            "revision": value.revision,
            "name": value.name,
            "slug": value.slug,
            "version": value.version,
            "labels": value.labels,
            "types": list(value.types),
        }

    def set_loaded_relations(
        self,
        loaded_relations: dict[str, bool],
        *,
        on_unloaded: Literal["silent", "warn", "raise"] = "warn",
    ) -> "ResourceSchema":
        LoadAwareMixin.set_loaded_relations(self, loaded_relations, on_unloaded=on_unloaded)
        return self

    def is_loaded(self, relation: str) -> bool:
        private = getattr(self, "__pydantic_private__", None) or {}
        return private.get("_loaded_relations", {}).get(relation, False)

    def require_loaded(self, relation: str) -> None:
        self._handle_unloaded(relation, f"include('{relation}')")

    def _handle_unloaded(self, field_name: str, include_hint: str) -> None:
        private = getattr(self, "__pydantic_private__", None) or {}
        loaded_relations = private.get("_loaded_relations", {})
        if loaded_relations.get(field_name, True):
            return
        message = (
            f"'{field_name}' was not loaded for ResourceSchema; "
            f"use {include_hint} or load='eager'."
        )
        on_unloaded = private.get("_on_unloaded", "warn")
        warned = private.setdefault("_warned_unloaded", set())
        if on_unloaded == "raise":
            raise UnloadedFieldError(message)
        if on_unloaded == "warn" and field_name not in warned:
            warnings.warn(message, UnloadedFieldWarning, stacklevel=3)
            warned.add(field_name)

    def __getattribute__(self, name: str):
        if name in object.__getattribute__(self, "_relation_fields"):
            self._handle_unloaded(name, f"include('{name}')")
        return super().__getattribute__(name)

    @model_validator(mode="after")
    def build_property_model(self) -> "ResourceSchema":
        """Convert the raw ``properties`` dict into a dynamic Pydantic model.

        After the initial parse, ``properties`` is a mapping of group slug →
        :class:`PropertySchema`.  This validator replaces it with a
        dynamically-created Pydantic model so that groups can be accessed as
        normal attributes (``resource.properties.dimensions``).

        Idempotent: if ``properties`` is already a :class:`~pydantic.BaseModel`
        the validator returns immediately.
        """
        if isinstance(self.properties, BaseModel):
            return self


        prop_fields: list[tuple[str, str]] = []
        prop_values: dict[str, PropertySchema] = {}
        for prop in self.properties.values():
            tmpl = prop.template
            field_name = getattr(tmpl, "slug", None) or tmpl.name
            prop_fields.append((field_name, tmpl.name))
            prop_values[field_name] = prop

        if prop_fields:
            model = build_property_groups_model(
                self.template.slug or self.template.name,
                tuple(prop_fields),
            )
            # PropertySchema instances were already validated while being
            # hydrated. Revalidating them here can coerce mutable array values
            # repeatedly instead of preserving them.
            self.properties = model.model_construct(**prop_values)


        return self


class ResourceRef(NamespaceOwnedFields):
    """Lightweight reference to a resource, containing only identity and template info.

    Used to represent parent/ancestor relationships without embedding the full
    :class:`ResourceSchema` (which would cause circular serialisation).

    Attributes:
        name: Display name of the referenced resource.
        template: Lightweight :class:`ResourceTemplateRef` for the resource's
            template.
    """

    name: Annotated[str, SIMPLE_FIELD]
    copied_from_id: Annotated[UUID | None, SIMPLE_FIELD] = None
    template: ResourceTemplateRef


class ResourceAssignmentSchema(BaseModel):
    """Represents the assignment of a resource to a slot in a process run.

    When a :class:`~recap.schemas.process.ProcessRunSchema` is executed,
    resources are bound to the resource slots declared on the process template.
    Each :class:`ResourceAssignmentSchema` captures one such binding.

    Attributes:
        slot: The :class:`ResourceSlotSchema` the resource is assigned to.
        resource: The :class:`ResourceSchema` that was assigned.
        step_id: UUID of the specific step this assignment is scoped to,
            or ``None`` if the assignment spans the entire process run.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
    slot: ResourceSlotSchema
    resource: ResourceSchema
    step_id: Annotated[UUID | None, SIMPLE_FIELD] = None


# References remain import-compatible names, but no second persisted model is
# created. Partial payloads use canonical classes with unloaded relations.
ResourceTemplateRef = ResourceTemplateSchema
ResourceRef = ResourceSchema  # noqa: F811
ResourceTemplateSchema.model_rebuild(force=True)
ResourceSchema.model_rebuild(force=True)
