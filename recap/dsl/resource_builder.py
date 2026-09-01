import json
import warnings
from datetime import UTC, datetime
from typing import Any, Literal, Optional, overload
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, create_model

from recap.client.backend import ClientBackend
from recap.commands.models import (
    CommandContext,
    CopyResource,
    CreateResource,
    CreateResourceTemplate,
    SetLifecycleStatus,
    UpdateResource,
    UpdateResourceTemplate,
)
from recap.db.resource import Resource
from recap.dsl.attribute_builder import AttributeGroupBuilder
from recap.dsl.drafts import (
    AttributeDraft,
    AttributeGroupDraft,
    ResourceTemplateDraft,
    detached_model,
)
from recap.dsl.query import QuerySpec
from recap.exceptions import (
    ExistingResourceError,
    ExistingResourceTemplateError,
    ExistingResourceTemplateWarning,
    ExistingResourceWarning,
    RecapNotFoundError,
)
from recap.lifecycle import LifecycleStatus
from recap.schemas.namespace import NamespaceContext
from recap.schemas.resource import (
    ResourceCopyChanges,
    ResourceCopyOptions,
    ResourceSchema,
    ResourceTemplateRef,
    ResourceTemplateSchema,
    ResourceTypeSchema,
)
from recap.utils.dsl import AliasMixin, lock_instance_fields, map_dtype_to_pytype


class ResourceBuilder:
    def __init__(  # noqa: C901
        self,
        # session: Session,
        name: str | None,
        template_name: str | None,
        template_version: str = "1.0",
        *,
        backend: ClientBackend,
        namespace_context: NamespaceContext,
        parent: "ResourceBuilder | ResourceSchema | None" = None,
        resource_id: UUID | None = None,
        on_existing: Literal["create", "silent", "warn", "raise"] = "warn",
        command_context: CommandContext,
    ):
        if command_context is None:
            raise ValueError("ResourceBuilder requires command context")
        self.name = name
        self.namespace_context = namespace_context
        self._children: list[Resource] = []
        self.parent = None
        self.parent_resource = None
        self.template_name = template_name
        self.template_version = template_version
        if on_existing not in {"create", "silent", "warn", "raise"}:
            raise ValueError(
                "on_existing must be one of: 'create', 'silent', 'warn', 'raise'"
            )
        self.on_existing = on_existing
        self._command_context = command_context
        self.backend = backend
        self._submitted = False
        self._last_properties_payload = None
        self._initial_properties_payload = None
        self._reused_existing = False
        self._expected_revision = 1
        self._is_new_resource = resource_id is None
        self._resource: ResourceSchema | None = None
        self._draft: ResourceSchema | None = None
        self._configure_parent(parent)
        if resource_id is not None:
            self._load_existing_resource(resource_id)
        else:
            self._prepare_new_resource()

    def _configure_parent(self, parent: "ResourceBuilder | ResourceSchema | None"):
        if isinstance(parent, self.__class__):
            self.parent = parent
            self.parent_resource = parent._resource if parent else None
        elif isinstance(parent, ResourceSchema):
            self.parent_resource = parent

    def _load_existing_resource(self, resource_id: UUID):
        self._resource = self._reload_resource(resource_id)
        self._draft = self._resource.model_copy(deep=True)
        self._expected_revision = self._resource.revision
        self._is_new_resource = False
        self._last_properties_payload = self._resource_properties_payload()
        self._submitted = True
        self.name = self._resource.name
        self.template_name = self._resource.template.name
        self.template_version = self._resource.template.version

    def _prepare_new_resource(self):
        if self.name is None or self.template_name is None:
            raise ValueError("name and template_name are required")
        templates = self.backend.query(
            ResourceTemplateSchema,
            QuerySpec(
                filters={
                    "name": self.template_name,
                    "version": self.template_version,
                },
                include_mutable=True,
                load_mode="eager",
            ),
            namespace_path=self.namespace_context.path,
        )
        if not templates:
            raise RecapNotFoundError(
                f"Resource template {self.template_name!r} version "
                f"{self.template_version!r} not found"
            )
        template = templates[0]
        self._template_id = template.id
        if isinstance(template, ResourceTemplateSchema):
            self._resource = self._draft_resource(template)
            self._draft = self._resource.model_copy(deep=True)
            self._initial_properties_payload = self._resource_properties_payload(
                self._draft
            )
        if self.on_existing != "create":
            parent_id = self.parent_resource.id if self.parent_resource else None
            matches = self.backend.query(
                ResourceSchema,
                QuerySpec(
                    filters={"name": self.name},
                    preloads=["template", "parent", "children", "properties"],
                    include_mutable=True,
                ),
                namespace_path=self.namespace_context.path,
            )
            matches = [
                match
                for match in matches
                if match.template.id == template.id
                and (match.parent.id if match.parent else None) == parent_id
            ]
            if matches:
                if self.on_existing == "raise":
                    raise ExistingResourceError(
                        f"Resource {self.name!r} already exists"
                    )
                if self.on_existing == "warn":
                    warnings.warn(
                        f"Resource {self.name!r} already exists and will be reused; "
                        "no new resource will be created.",
                        ExistingResourceWarning,
                        stacklevel=2,
                    )
                self._resource = ResourceSchema.model_validate(matches[0])
                self._draft = self._resource.model_copy(
                    deep=True, update={"id": self._resource.id}
                )
                self._expected_revision = self._resource.revision
                self._is_new_resource = False
                self._reused_existing = True
                self._last_properties_payload = self._resource_properties_payload()
                self._submitted = True

    def _draft_resource(self, template: ResourceTemplateSchema) -> ResourceSchema:
        properties = {}
        for group in template.attribute_group_templates:
            properties[group.name] = {
                attribute.name: {
                    "value": (
                        json.loads(attribute.default_value)
                        if attribute.value_type == "array"
                        and isinstance(attribute.default_value, str)
                        else attribute.default_value
                    ),
                    "unit": attribute.unit,
                    "metadata_json": attribute.metadata or {},
                }
                for attribute in group.attribute_templates
            }
            properties[group.name] = {
                "template": group,
                "values": properties[group.name],
            }
        return ResourceSchema.model_construct(
            id=uuid4(),
            name=self.name,
            template=template,
            children={},
            properties={},
            namespace_id=self.namespace_context.id,
            revision=1,
            status=LifecycleStatus.MUTABLE,
        )

    @staticmethod
    def _property_schema(value):
        from recap.schemas.resource import PropertySchema

        now = datetime.now(UTC)
        return PropertySchema.model_validate(
            {"id": UUID(int=0), "create_date": now, "modified_date": now, **value}
        )

    @classmethod
    def create(
        cls,
        name: str,
        template_name: str,
        template_version: str,
        *,
        backend: ClientBackend,
        namespace_context: NamespaceContext,
        command_context: CommandContext | None = None,
        parent=None,
        on_existing: Literal["create", "silent", "warn", "raise"] = "create",
    ):
        builder = cls(
            name,
            template_name,
            template_version,
            backend=backend,
            namespace_context=namespace_context,
            command_context=command_context,
            parent=parent,
            on_existing=on_existing,
        )
        builder.save()
        return builder._resource

    def __enter__(self):
        if self._is_new_resource and self._resource is not None:
            self.save()
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.save()

    def save(self):
        if self._draft is not None and self._resource is not None:
            source = (
                self._draft
                if isinstance(self._resource, ResourceSchema)
                else self._resource
            )
            properties = self._resource_properties_payload(source)
        else:
            properties = self._resource_properties_payload()
        if (
            self._submitted
            and properties == self._last_properties_payload
            and (
                self._draft is None
                or self._resource is None
                or self._draft.name == self._resource.name
            )
        ):
            return self
        if self._is_new_resource:
            if properties == self._initial_properties_payload:
                properties = None
            command = CreateResource(
                id=getattr(self._resource, "id", None),
                namespace_path=self.namespace_context.path,
                name=self.name,
                template_id=self._template_id,
                parent_id=self.parent_resource.id if self.parent_resource else None,
                properties=properties,
            )
        elif self._resource.status is not LifecycleStatus.MUTABLE:
            command = CopyResource(
                source_resource_id=self._resource.id,
                destination_namespace_path=self.namespace_context.path,
                options=ResourceCopyOptions(
                    name=self._draft.name
                    if self._draft is not None
                    else self._resource.name,
                    changes=ResourceCopyChanges(properties=properties),
                ),
            )
        else:
            command = UpdateResource(
                resource_id=self._resource.id,
                expected_revision=self._expected_revision,
                name=self._draft.name
                if self._draft is not None
                else self._resource.name,
                properties=properties,
            )
        result = self.backend._execute(command, self._command_context)
        if not isinstance(result, ResourceSchema):
            return self
        self._resource = result
        self._draft = result.model_copy(deep=True)
        self._expected_revision = result.revision
        self._is_new_resource = False
        self._last_properties_payload = self._resource_properties_payload(result)
        self._submitted = True
        return self

    def _resource_properties_payload(self, resource=None):
        resource = resource or self._resource
        if resource is None:
            return None
        payload = {}
        for group_name, prop in resource.properties.items():
            value_names = (
                type(prop.values).model_fields
                if isinstance(prop.values, BaseModel)
                else vars(prop.values)
            )
            payload[group_name] = {
                value_name: {
                    "value": getattr(prop.values, value_name).value,
                    "unit": getattr(prop.values, value_name).unit,
                    "metadata_json": getattr(prop.values, value_name).metadata_json,
                }
                for value_name in value_names
            }
        return payload

    def activate(self):
        if self._is_new_resource:
            self.save()
        result = self.backend._execute(
            SetLifecycleStatus(
                object_type="resource",
                object_id=self.resource.id,
                expected_revision=self._resource.revision,
                status=LifecycleStatus.ACTIVE.value,
            ),
            self._command_context,
        )
        if isinstance(result, ResourceSchema):
            self._resource = result
            self._draft = result.model_copy(deep=True)
            self._expected_revision = result.revision
        elif result is not None:
            return self
        return self

    def archive(self):
        if self._is_new_resource:
            self.save()
        result = self.backend._execute(
            SetLifecycleStatus(
                object_type="resource",
                object_id=self.resource.id,
                expected_revision=self._resource.revision,
                status=LifecycleStatus.ARCHIVED.value,
            ),
            self._command_context,
        )
        if isinstance(result, ResourceSchema):
            self._resource = result
            self._draft = result.model_copy(deep=True)
            self._expected_revision = result.revision
        elif result is not None:
            return self
        return self

    def _reload_resource(self, resource_id: UUID) -> ResourceSchema:
        resources = self.backend.query(
            ResourceSchema,
            QuerySpec(
                filters={"id": resource_id},
                preloads=["template", "parent", "children", "properties"],
                include_mutable=True,
            ),
            namespace_path=self.namespace_context.path,
        )
        if not resources:
            raise RecapNotFoundError(f"Resource with id {resource_id} not found")
        return resources[0]

    @property
    def resource(self) -> ResourceSchema:
        if self._draft is None:
            raise RuntimeError(
                "Call .save() first or construct resource via builder methods"
            )
        return self._draft

    def get_model(self, *, update: bool = False) -> ResourceSchema:
        """
        Return a pydantic model representing the resource, optionally reloading
        from the backend first. Critical fields are locked against mutation.
        """
        if update and self._resource:
            self._resource = self._reload_resource(self._resource.id)
            self._expected_revision = self._resource.revision
            self._draft = self._resource.model_copy(deep=True)
        model = (self._draft or self.resource).model_copy(deep=True)
        return lock_instance_fields(
            model, {"id", "create_date", "modified_date", "slug", "template"}
        )

    def set_model(self, model: ResourceSchema):
        if self.resource.id != model.id:
            raise ValueError(
                "ID for this Resource does not match the builder's resource"
            )
        self._draft = detached_model(model)
        self._submitted = False

    @overload
    def add_child(
        self, name: str, template_name: str, template_version: str = "1.0"
    ) -> "ResourceBuilder": ...

    @overload
    def add_child(self, source: UUID | ResourceSchema) -> "ResourceBuilder": ...

    def add_child(
        self,
        name_or_source: str | UUID | ResourceSchema,
        template_name: str | None = None,
        template_version: str = "1.0",
    ) -> "ResourceBuilder":
        if isinstance(name_or_source, (UUID, ResourceSchema)):
            if template_name is not None or template_version != "1.0":
                raise TypeError("Copied child accepts exactly one source argument")
            source_id = (
                name_or_source.id
                if isinstance(name_or_source, ResourceSchema)
                else name_or_source
            )
            copied = self.backend._execute(
                CopyResource(
                    source_resource_id=source_id,
                    destination_namespace_path=self.namespace_context.path,
                    options=ResourceCopyOptions(parent_id=self.resource.id),
                ),
                self._command_context,
            )
            if not isinstance(copied, ResourceSchema):
                raise RuntimeError("Copy resource command did not return a resource")
            child_builder = ResourceBuilder(
                name=None,
                template_name=None,
                backend=self.backend,
                namespace_context=self.namespace_context,
                command_context=self._command_context,
                parent=self,
                resource_id=copied.id,
            )
            self._draft.children[copied.name] = child_builder.resource
            return child_builder
        if template_name is None:
            raise TypeError("New child requires name and template_name")
        child_builder = ResourceBuilder(
            name=name_or_source,
            template_name=template_name,
            template_version=template_version,
            namespace_context=self.namespace_context,
            backend=self.backend,
            command_context=self._command_context,
            parent=self,
        )
        child_builder.save()
        self._draft.children[name_or_source] = child_builder.resource
        return child_builder

    def close_child(self):
        if self.parent:
            return self.parent
        else:
            return self

    def get_props(self) -> type[BaseModel]:
        props: dict[str, tuple] = {
            "resource_name": (
                Literal[self.resource.name],
                Field(default=self.resource.name),
            ),
            "resource_id": (UUID, Field(default=self.resource.id)),
        }
        for _, prop in self._resource.properties.items():
            prop_fields: dict[str, tuple] = {}
            for val_name, value in prop.items():
                value_template = None
                for vt in prop.template.attribute_templates:
                    if vt.name == val_name:
                        value_template = vt
                        break
                if value_template is None:
                    raise ValueError(f"Could not find value with {val_name}")
                raw_value = getattr(value, "value", value)
                pytype = map_dtype_to_pytype(value_template.value_type)
                prop_fields[value_template.slug] = (
                    pytype | None,
                    Field(default=raw_value, alias=value_template.name),
                )
                prop_model = create_model(
                    f"{val_name}", **prop_fields, __base__=(AliasMixin, BaseModel)
                )
                props[prop.template.slug] = (
                    prop_model,
                    Field(default_factory=prop_model, alias=prop.template.name),
                )
        model = create_model(
            f"{self.resource.name}", **props, __base__=(AliasMixin, BaseModel)
        )
        return model()

    def set_props(self, filled_props):
        if self.resource is None:
            raise ValueError("Resource not setup")
        for prop in self._draft.properties.values():
            filled_prop = filled_props.get(prop.template.name)
            for value_name in self.resource.properties[prop.template.name].values:
                self.resource.properties[prop.template.name].values[value_name] = (
                    filled_prop.get(value_name)
                )


class ResourceTemplateBuilder:
    def __init__(  # noqa: C901
        self,
        name: str | None,
        type_names: list[str] | None = None,
        version: str = "1.0",
        parent: Optional["ResourceTemplateBuilder"] = None,
        *,
        backend: ClientBackend,
        namespace_context: NamespaceContext,
        resource_template_id: UUID | None = None,
        on_existing: Literal["silent", "warn", "raise"] = "warn",
        command_context: CommandContext,
    ):
        self.namespace_context = namespace_context
        self._command_context = command_context
        self.backend = backend
        self._submitted = False
        self._last_draft = None
        self._expected_revision = 1
        self._draft_groups: list[AttributeGroupDraft] = []
        self._draft_children: list[ResourceTemplateBuilder] = []
        self.name = name
        self.type_names = type_names
        self._children: list[ResourceTemplateRef] = []
        self.parent = parent
        self.resource_types: dict[str, ResourceTypeSchema] = {}
        self.version = version
        if on_existing not in {"silent", "warn", "raise"}:
            raise ValueError("on_existing must be one of: 'silent', 'warn', 'raise'")
        self.on_existing = on_existing
        self._template: ResourceTemplateRef | ResourceTemplateSchema | None = None
        self._is_new_template = resource_template_id is None
        self._draft_model: ResourceTemplateSchema | None = None
        if resource_template_id is not None:
            self._initialize_command_update(resource_template_id)
        elif name is None or type_names is None:
            raise ValueError(
                "name and type_names are required to create a resource template"
            )
        else:
            self._template = ResourceTemplateRef.model_construct(
                id=uuid4(),
                create_date=None,
                modified_date=None,
                namespace_id=self.namespace_context.id,
                status=LifecycleStatus.MUTABLE,
                revision=1,
                name=name,
                slug=None,
                version=version,
                labels=[],
                types=[],
            )
            existing = self.backend.query(
                ResourceTemplateSchema,
                QuerySpec(
                    filters={"name": name, "version": version},
                    include_mutable=True,
                    load_mode="eager",
                ),
                namespace_path=self.namespace_context.path,
            )
            if existing:
                if on_existing == "raise":
                    raise ExistingResourceTemplateError(
                        f"Resource template {name!r} version {version!r} already exists"
                    )
                if on_existing == "warn":
                    warnings.warn(
                        f"Resource template {name!r} version {version!r} already exists and will be reused; bump the version",
                        ExistingResourceTemplateWarning,
                        stacklevel=2,
                    )
                self._initialize_command_update(existing[0].id)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.save()

    def save(self):
        draft = self._build_draft()
        if self._submitted and draft == self._last_draft:
            return self
        if self._is_new_template:
            command = CreateResourceTemplate(
                namespace_path=self.namespace_context.path, draft=draft
            )
        else:
            command = UpdateResourceTemplate(
                template_id=self._template.id,
                expected_revision=self._expected_revision,
                draft=draft,
            )
        result = self.backend._execute(command, self._command_context)
        if result is None:
            return self
        self._template = result
        self._expected_revision = result.revision
        self._last_draft = draft
        self._submitted = True
        return self

    def update_status(self, status: LifecycleStatus):
        if self._template is None:
            self.save()
        result = self.backend._execute(
            SetLifecycleStatus(
                object_type="resource_template",
                object_id=self.template.id,
                expected_revision=self._template.revision,
                status=status.value,  # LifecycleStatus.ACTIVE.value,
            ),
            self._command_context,
        )
        if isinstance(result, ResourceTemplateSchema):
            self._template = result
        return self

    def activate(self):
        self.update_status(LifecycleStatus.ACTIVE)

    def archive(self):
        self.update_status(LifecycleStatus.ARCHIVED)

    @property
    def template(self) -> ResourceTemplateRef:
        if self._template is None:
            raise RuntimeError(
                "Call .save() first or construct template via builder methods"
            )
        return self._template

    def prop_group(
        self, group_name: str
    ) -> AttributeGroupBuilder["ResourceTemplateBuilder"]:
        existing = next(
            (draft for draft in self._draft_groups if draft.name == group_name), None
        )
        if existing is not None:
            return _DraftResourceAttributeGroupBuilder(group_name, self, existing)
        return _DraftResourceAttributeGroupBuilder(group_name, self)

    def add_properties(
        self, prop_def: dict[str, list[dict[str, Any]]]
    ) -> "ResourceTemplateBuilder":
        """Add property groups and their attributes to this resource template.

        Args:
            prop_def: A mapping of group name → list of attribute dicts.  Each
                attribute dict accepts the keys ``name``, ``type``,
                ``default``, ``unit`` (optional), and ``metadata`` (optional).

        Example::

            template_builder.add_properties({
                "content": [
                    {"name": "catalog_id", "type": "str", "default": ""},
                    {"name": "volume", "type": "float", "default": 10.0, "unit": "uL"},
                ]
            })

        Returns:
            ``self``, to allow method chaining.
        """
        self._draft_groups.extend(
            AttributeGroupDraft(
                name=group_key,
                attributes=[
                    AttributeDraft(
                        name=prop["name"],
                        type=prop["type"],
                        unit=prop.get("unit", ""),
                        default=prop.get("default"),
                        metadata=prop.get("metadata", {}),
                    )
                    for prop in props
                ],
            )
            for group_key, props in prop_def.items()
        )
        return self

    def add_child(
        self, name: str, type_names: list[str], version: str = "1.0"
    ) -> "ResourceTemplateBuilder":
        child_builder = ResourceTemplateBuilder(
            name=name,
            type_names=type_names,
            version=version,
            parent=self,
            namespace_context=self.namespace_context,
            backend=self.backend,
            command_context=self._command_context,
        )
        self._draft_children.append(child_builder)
        return child_builder

    def _reload_template(self):
        templates = self.backend.query(
            ResourceTemplateSchema,
            QuerySpec(
                filters={"id": self._template.id if self._template else None},
                include_mutable=True,
                load_mode="eager",
            ),
            namespace_path=self.namespace_context.path,
        )
        if not templates:
            raise RecapNotFoundError("Resource template not found")
        self._template = templates[0]

    def get_model(self, *, update: bool = False) -> ResourceTemplateSchema:
        """
        Return a pydantic model for the resource template, optionally reloading
        from the backend first. Critical fields are locked against mutation.
        """
        if update or self._template is None:
            self.save()
        if not isinstance(self._template, ResourceTemplateSchema):
            raise RuntimeError("Command backend did not return resource template")
        return lock_instance_fields(
            self._template.model_copy(deep=True),
            {"id", "create_date", "modified_date", "version"},
        )

    def set_model(self, model: ResourceTemplateSchema | ResourceTemplateRef):
        if self._template is None:
            raise RuntimeError("ResourceTemplate not initialized")
        if model.id != self._template.id:
            raise ValueError(
                "ID for this ResourceTemplate does not match the builder's template"
            )
        self._draft_model = detached_model(model)
        if isinstance(model, ResourceTemplateSchema):
            self.name = model.name
            self.version = model.version
            self.type_names = [resource_type.name for resource_type in model.types]
        self._submitted = False

    def close_child(self):
        if self.parent:
            return self.parent
        else:
            return self

    def _build_draft(self) -> ResourceTemplateDraft:
        if self._draft_model is not None:
            model = self._draft_model
            return ResourceTemplateDraft(
                id=model.id,
                name=model.name,
                version=model.version,
                labels=model.labels,
                type_names=[resource_type.name for resource_type in model.types],
                property_groups=[
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
                    for group in model.attribute_group_templates
                ],
                children=[
                    ResourceTemplateBuilder._draft_from_model(child)
                    for child in model.children.values()
                ],
            )
        return ResourceTemplateDraft(
            id=self._template.id,
            name=self.name,
            version=self.version,
            type_names=self.type_names or (),
            property_groups=self._draft_groups,
            children=[child._build_draft() for child in self._draft_children],
        )

    @staticmethod
    def _draft_from_model(model: ResourceTemplateSchema) -> ResourceTemplateDraft:
        return ResourceTemplateDraft(
            id=model.id,
            name=model.name,
            version=model.version,
            labels=model.labels,
            type_names=[resource_type.name for resource_type in model.types],
            property_groups=[
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
                for group in model.attribute_group_templates
            ],
            children=[
                ResourceTemplateBuilder._draft_from_model(child)
                for child in model.children.values()
            ],
        )

    def _initialize_command_update(self, resource_template_id: UUID) -> None:
        self._is_new_template = False
        templates = self.backend.query(
            ResourceTemplateSchema,
            QuerySpec(
                filters={"id": resource_template_id},
                include_mutable=True,
                load_mode="eager",
            ),
            namespace_path=self.namespace_context.path,
        )
        if not templates:
            raise RecapNotFoundError(
                f"ResourceTemplate with id {resource_template_id} not found"
            )
        template = templates[0]
        self._template = template
        self.name = template.name
        self.version = template.version
        self._expected_revision = template.revision
        self.type_names = [resource_type.name for resource_type in template.types]
        self._draft_groups = [
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
            for group in template.attribute_group_templates
        ]
        self._last_draft = self._build_draft()
        self._submitted = True


class _DraftResourceAttributeGroupBuilder:
    def __init__(self, group_name: str, parent: ResourceTemplateBuilder, draft=None):
        self.parent = parent
        self._draft = draft or AttributeGroupDraft(name=group_name, attributes=[])

    def add_attribute(
        self,
        attr_name: str,
        value_type: str,
        unit: str,
        default: Any,
        metadata: dict[str, Any] | None = None,
    ):
        if any(attribute.name == attr_name for attribute in self._draft.attributes):
            return self
        self._draft = self._draft.model_copy(
            update={
                "attributes": self._draft.attributes
                + (
                    AttributeDraft(
                        name=attr_name,
                        type=value_type,
                        unit=unit,
                        default=default,
                        metadata=metadata or {},
                    ),
                )
            }
        )
        return self

    def close_group(self):
        for index, draft in enumerate(self.parent._draft_groups):
            if draft.name == self._draft.name:
                self.parent._draft_groups[index] = self._draft
                break
        else:
            self.parent._draft_groups.append(self._draft)
        return self.parent
