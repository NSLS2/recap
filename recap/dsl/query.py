import warnings
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeVar, cast
from uuid import UUID

from pydantic import BaseModel, field_validator
from pydantic import Field as PydanticField

from recap.schemas.namespace import NamespaceContext, NamespaceSchema
from recap.schemas.resource import (
    ResourceRef,
    ResourceSchema,
    ResourceTemplateRef,
    ResourceTemplateSchema,
)

if TYPE_CHECKING:
    from recap.adapter import Backend
from recap.schemas.process import (
    ProcessRunRef,
    ProcessRunSchema,
    ProcessTemplateRef,
    ProcessTemplateSchema,
)

try:
    from typing import Self
except ModuleNotFoundError:
    from typing_extensions import Self  # noqa


SchemaT = TypeVar("SchemaT", bound=BaseModel)
ModelT = TypeVar("ModelT")
OnUnloadedPolicy = Literal["silent", "warn", "raise"]
Shape = Literal["full", "ref"]
ShapeInput = Literal["full", "ref", "schema"]
LoadMode = Literal["none", "eager"]
LoadInput = Literal["none", "eager", "full"]
FieldOperator = Literal[
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "not_in",
    "contains",
    "starts_with",
    "ends_with",
]


def _normalize_shape(shape: ShapeInput) -> Shape:
    if shape == "schema":
        warnings.warn(
            "shape='schema' is deprecated; use shape='full' instead",
            DeprecationWarning,
            stacklevel=3,
        )
        return "full"
    if shape not in ("full", "ref"):
        raise ValueError("shape must be one of 'full', 'ref', or deprecated 'schema'")
    return cast(Shape, shape)


def _normalize_load(load: LoadInput) -> LoadMode:
    if load == "full":
        warnings.warn(
            "load='full' is deprecated; use load='eager' instead",
            DeprecationWarning,
            stacklevel=3,
        )
        return "eager"
    if load not in ("none", "eager"):
        raise ValueError("load must be one of 'none', 'eager', or deprecated 'full'")
    return cast(LoadMode, load)


def _validate_field_path(path: str) -> str:
    if (
        not isinstance(path, str)
        or not path
        or any(not part for part in path.split("."))
    ):
        raise ValueError("field path must contain non-empty dot-separated names")
    return path


class FieldPredicate(BaseModel):
    field: str
    op: FieldOperator
    value: Any

    _validate_field = field_validator("field")(_validate_field_path)

    def __bool__(self) -> bool:
        raise TypeError(
            "Field predicates cannot use Python 'and' or 'or'; "
            "chain where() calls to combine predicates with AND"
        )


class FieldOrdering(BaseModel):
    field: str
    direction: Literal["asc", "desc"] = "asc"

    _validate_field = field_validator("field")(_validate_field_path)


class Field:
    def __init__(self, path: str):
        self.path = _validate_field_path(path)

    def _predicate(self, op: FieldOperator, value: Any) -> FieldPredicate:
        return FieldPredicate(field=self.path, op=op, value=value)

    def __eq__(self, value: Any) -> FieldPredicate:  # type: ignore[override]
        return self._predicate("eq", value)

    def __ne__(self, value: Any) -> FieldPredicate:  # type: ignore[override]
        return self._predicate("ne", value)

    def __lt__(self, value: Any) -> FieldPredicate:
        return self._predicate("lt", value)

    def __le__(self, value: Any) -> FieldPredicate:
        return self._predicate("lte", value)

    def __gt__(self, value: Any) -> FieldPredicate:
        return self._predicate("gt", value)

    def __ge__(self, value: Any) -> FieldPredicate:
        return self._predicate("gte", value)

    def in_(self, values: Sequence[Any]) -> FieldPredicate:
        return self._membership_predicate("in", values)

    def not_in(self, values: Sequence[Any]) -> FieldPredicate:
        return self._membership_predicate("not_in", values)

    def _membership_predicate(
        self, op: Literal["in", "not_in"], values: Sequence[Any]
    ) -> FieldPredicate:
        if isinstance(values, str) or not isinstance(values, Sequence):
            raise TypeError("membership values must be a non-string sequence")
        return self._predicate(op, list(values))

    def contains(self, value: str) -> FieldPredicate:
        return self._predicate("contains", value)

    def starts_with(self, value: str) -> FieldPredicate:
        return self._predicate("starts_with", value)

    def ends_with(self, value: str) -> FieldPredicate:
        return self._predicate("ends_with", value)

    def asc(self) -> FieldOrdering:
        return FieldOrdering(field=self.path, direction="asc")

    def desc(self) -> FieldOrdering:
        return FieldOrdering(field=self.path, direction="desc")


class PropertyFilter(BaseModel):
    name: str
    group: str | None = None
    op: Literal["eq", "gt", "gte", "lt", "lte", "between", "in"] = "eq"
    value: Any
    upper: Any | None = None
    value_type: str | None = None

    def model_dump(self, *args, **kwargs):
        # Ensure upper/value serialize even when None for clarity in REST payloads
        kwargs.setdefault("exclude_none", False)
        return super().model_dump(*args, **kwargs)


class ParameterFilter(BaseModel):
    name: str
    group: str | None = None
    step: str | None = None
    op: Literal["eq", "gt", "gte", "lt", "lte", "between", "in"] = "eq"
    value: Any
    upper: Any | None = None
    value_type: str | None = None

    def model_dump(self, *args, **kwargs):
        kwargs.setdefault("exclude_none", False)
        return super().model_dump(*args, **kwargs)


class QuerySpec(BaseModel):
    filters: dict[str, Any] = {}
    predicates: Sequence[Any] = ()
    orderings: Sequence[Any] = ()
    preloads: Sequence[str] = ()
    limit: int | None = None
    offset: int | None = None
    property_filters: list[PropertyFilter] = PydanticField(default_factory=list)
    parent_resource_id: UUID | None = None
    parameter_filters: list[ParameterFilter] = PydanticField(default_factory=list)
    include_archived: bool = False
    include_mutable: bool = PydanticField(default=False, exclude=True)
    local_metadata_filters: dict[str, Any] = PydanticField(default_factory=dict)
    effective_metadata_filters: dict[str, Any] = PydanticField(default_factory=dict)
    load_mode: LoadMode | None = None
    on_unloaded: OnUnloadedPolicy | None = None

    @field_validator("load_mode", mode="before")
    @classmethod
    def _normalize_load_mode(cls, value):
        if value is None:
            return None
        return _normalize_load(value)

    @field_validator("predicates", mode="before")
    @classmethod
    def _normalize_predicates(cls, values):
        if isinstance(values, Sequence) and not isinstance(values, str):
            return [
                FieldPredicate.model_validate(value)
                if isinstance(value, Mapping)
                else value
                for value in values
            ]
        return values

    @field_validator("orderings", mode="before")
    @classmethod
    def _normalize_orderings(cls, values):
        if isinstance(values, Sequence) and not isinstance(values, str):
            return [
                FieldOrdering.model_validate(value)
                if isinstance(value, Mapping)
                else value
                for value in values
            ]
        return values


class BaseQuery(Generic[SchemaT]):
    schema: type[SchemaT]

    def __init__(
        self: Self,
        backend: "Backend",
        *,
        context: NamespaceContext,
        model: type[SchemaT] | None = None,
        filters: dict[str, Any] | None = None,
        predicates: list[Any] | None = None,
        orderings: list[Any] | None = None,
        preloads: list[str] | None = None,
        limit: int | None = None,
        offset: int | None = None,
        property_filters: list[PropertyFilter] | None = None,
        parent_resource_id: UUID | None = None,
        parameter_filters: list[ParameterFilter] | None = None,
        include_archived: bool = False,
        local_metadata_filters: dict[str, Any] | None = None,
        effective_metadata_filters: dict[str, Any] | None = None,
        load_mode: LoadMode | None = None,
        on_unloaded: OnUnloadedPolicy | None = None,
    ):
        self._backend = backend
        self._context = context
        self.model: type[SchemaT] = model or self.__class__.model  # type: ignore[attr-defined]
        self._filters = filters or {}
        self._predicates = predicates or []
        self._orderings = orderings or []
        self._preloads = preloads or []
        self._limit = limit
        self._offset = offset
        self._property_filters = property_filters or []
        self._parent_resource_id = parent_resource_id
        self._parameter_filters = parameter_filters or []
        self._include_archived = include_archived
        self._local_metadata_filters = local_metadata_filters or {}
        self._effective_metadata_filters = effective_metadata_filters or {}
        self._load_mode = load_mode
        self._on_unloaded = on_unloaded

    def _infer_value_type(self, value: Any) -> str | None:
        if isinstance(value, bool):
            return "bool"
        if isinstance(value, int) and not isinstance(value, bool):
            return "int"
        if isinstance(value, float):
            return "float"
        if isinstance(value, datetime):
            return "datetime"
        return "str"

    def _clone(self: Self, **overrides) -> "Self":
        """
        We can users to query data, not modify the object
        So for now we clone the sqlalchemy object and pass it
        to the user
        """
        params = dict(
            backend=self._backend,
            context=self._context,
            model=self.model,
            filters=dict(self._filters),
            predicates=list(self._predicates),
            orderings=list(self._orderings),
            preloads=list(self._preloads),
            limit=self._limit,
            offset=self._offset,
            property_filters=list(self._property_filters),
            parent_resource_id=self._parent_resource_id,
            parameter_filters=list(self._parameter_filters),
            include_archived=self._include_archived,
            local_metadata_filters=dict(self._local_metadata_filters),
            effective_metadata_filters=dict(self._effective_metadata_filters),
            load_mode=self._load_mode,
            on_unloaded=self._on_unloaded,
        )
        params.update(overrides)
        clone = self.__class__(**params)
        return clone

    def filter(self, **kwargs) -> "Self":
        new_filters = dict(self._filters)
        new_filters.update(kwargs)
        return self._clone(filters=new_filters)

    def where(self, *predicates) -> "Self":
        for predicate in predicates:
            if not isinstance(predicate, FieldPredicate):
                warnings.warn(
                    "Legacy query predicates are deprecated; use Field(...) instead",
                    DeprecationWarning,
                    stacklevel=2,
                )
        return self._clone(predicates=self._predicates + list(predicates))

    def order_by(self, *orderings) -> "Self":
        normalized = []
        for ordering in orderings:
            if isinstance(ordering, Field):
                normalized.append(ordering.asc())
            else:
                normalized.append(ordering)
                if not isinstance(ordering, FieldOrdering):
                    warnings.warn(
                        "Legacy query orderings are deprecated; use Field(...) instead",
                        DeprecationWarning,
                        stacklevel=2,
                    )
        return self._clone(orderings=self._orderings + normalized)

    def limit(self, value: int) -> "Self":
        return self._clone(limit=value)

    def offset(self, value: int) -> "Self":
        return self._clone(offset=value)

    def include_archived(self) -> "Self":
        return self._clone(include_archived=True)

    def include(self, relation_names: str | Sequence[str]) -> "Self":
        names = (
            [relation_names]
            if isinstance(relation_names, str)
            else list(relation_names)
        )
        new_preloads = list(self._preloads)
        for name in names:
            if name not in new_preloads:
                new_preloads.append(name)
        return self._clone(preloads=new_preloads)

    @property
    def _spec(self) -> QuerySpec:
        return QuerySpec(
            filters=self._filters,
            predicates=self._predicates,
            orderings=self._orderings,
            preloads=self._preloads,
            limit=self._limit,
            offset=self._offset,
            property_filters=self._property_filters,
            parent_resource_id=self._parent_resource_id,
            parameter_filters=self._parameter_filters,
            include_archived=self._include_archived,
            local_metadata_filters=self._local_metadata_filters,
            effective_metadata_filters=self._effective_metadata_filters,
            load_mode=self._load_mode,
            on_unloaded=self._on_unloaded,
        )

    def _execute(self) -> list[SchemaT]:
        rows = self._backend.query(
            self.model, self._spec, namespace_path=self._context.path
        )
        return rows

    def all(self) -> Sequence[SchemaT] | Sequence[BaseModel]:
        return self._execute()

    def first(self) -> SchemaT | None:
        return next(iter(self.limit(1)._execute()), None)

    def count(self) -> int:
        return self._backend.count(
            self.model, self._spec, namespace_path=self._context.path
        )


class NamespaceQuery(BaseQuery[NamespaceSchema]):
    model = NamespaceSchema

    def filter_local_metadata(self, **metadata: Any) -> "NamespaceQuery":
        values = dict(self._local_metadata_filters)
        values.update(metadata)
        return self._clone(local_metadata_filters=values)

    def filter_effective_metadata(self, **metadata: Any) -> "NamespaceQuery":
        values = dict(self._effective_metadata_filters)
        values.update(metadata)
        return self._clone(effective_metadata_filters=values)


class ProcessRunQuery(BaseQuery[ProcessRunSchema | ProcessRunRef]):
    model = ProcessRunSchema
    default_schema = None

    def __init__(
        self,
        backend: "Backend",
        *,
        shape: ShapeInput = "full",
        load: LoadInput = "none",
        on_unloaded: OnUnloadedPolicy | None = None,
        **kwargs,
    ):
        shape = _normalize_shape(shape)
        load = _normalize_load(load)
        if shape == "ref" and load != "none":
            raise ValueError("load must be 'none' when shape='ref'")
        self._shape = shape
        self._load = load
        model = ProcessRunSchema if shape == "full" else ProcessRunRef
        super().__init__(
            backend,
            model=model,
            load_mode=load,
            on_unloaded=on_unloaded,
            **kwargs,
        )

    def _clone(self, **overrides) -> "ProcessRunQuery":
        params = dict(
            backend=self._backend,
            context=self._context,
            shape=self._shape,
            load=self._load,
            filters=dict(self._filters),
            predicates=list(self._predicates),
            orderings=list(self._orderings),
            preloads=list(self._preloads),
            limit=self._limit,
            offset=self._offset,
            property_filters=list(self._property_filters),
            parent_resource_id=self._parent_resource_id,
            parameter_filters=list(self._parameter_filters),
            include_archived=self._include_archived,
            local_metadata_filters=dict(self._local_metadata_filters),
            effective_metadata_filters=dict(self._effective_metadata_filters),
            on_unloaded=self._on_unloaded,
        )
        params.update(overrides)
        return self.__class__(**params)

    def include(self, relation_names: str | Sequence[str]) -> "ProcessRunQuery":
        if self._shape == "ref":
            raise ValueError("include(...) is only valid when shape='full'")
        if self._load == "eager":
            raise ValueError("include(...) cannot be combined with load='eager'")
        return super().include(relation_names)

    def include_steps(self, *, include_parameters: bool = False) -> "ProcessRunQuery":
        if include_parameters:
            return self.include(["steps", "steps.parameters"])
        return self.include("steps")

    def filter_parameter(
        self,
        name: str,
        *,
        group: str | None = None,
        step: str | None = None,
        eq: Any | None = None,
        gt: Any | None = None,
        gte: Any | None = None,
        lt: Any | None = None,
        lte: Any | None = None,
        between: tuple[Any, Any] | None = None,
        in_: Sequence[Any] | None = None,
        value_type: str | None = None,
    ) -> "ProcessRunQuery":
        comparators = {
            "eq": eq,
            "gt": gt,
            "gte": gte,
            "lt": lt,
            "lte": lte,
            "between": between,
            "in": in_,
        }
        set_ops = {op for op, v in comparators.items() if v is not None}
        if len(set_ops) != 1:
            raise ValueError(
                "filter_parameter requires exactly one comparator (eq/gt/gte/lt/lte/between/in_)"
            )
        op = next(iter(set_ops))
        raw_value = comparators[op]

        if op == "between":
            if not isinstance(raw_value, Sequence) or len(raw_value) != 2:
                raise ValueError(
                    "between requires a 2-tuple/sequence of (lower, upper)"
                )
            lower, upper = raw_value
        else:
            lower, upper = raw_value, None

        if value_type is None:
            probe = lower if op != "between" else lower
            value_type = self._infer_value_type(probe)

        pf = ParameterFilter(
            name=name,
            group=group,
            step=step,
            op=op,  # type: ignore[arg-type]
            value=lower,
            upper=upper,
            value_type=value_type,
        )
        return self._clone(parameter_filters=self._parameter_filters + [pf])

    def include_resources(self) -> "ProcessRunQuery":
        return self.include("resources")


class ResourceQuery(BaseQuery[ResourceSchema | ResourceRef]):
    model = ResourceSchema
    default_schema = None

    def __init__(
        self,
        backend: "Backend",
        *,
        shape: ShapeInput = "full",
        load: LoadInput = "none",
        on_unloaded: OnUnloadedPolicy | None = None,
        **kwargs,
    ):
        shape = _normalize_shape(shape)
        load = _normalize_load(load)
        if shape == "ref" and load != "none":
            raise ValueError("load must be 'none' when shape='ref'")
        self._shape = shape
        self._load = load
        model = ResourceSchema if shape == "full" else ResourceRef
        super().__init__(
            backend,
            model=model,
            load_mode=load,
            on_unloaded=on_unloaded,
            **kwargs,
        )

    def _clone(self, **overrides) -> "ResourceQuery":
        params = dict(
            backend=self._backend,
            context=self._context,
            shape=self._shape,
            load=self._load,
            filters=dict(self._filters),
            predicates=list(self._predicates),
            orderings=list(self._orderings),
            preloads=list(self._preloads),
            limit=self._limit,
            offset=self._offset,
            property_filters=list(self._property_filters),
            parent_resource_id=self._parent_resource_id,
            parameter_filters=list(self._parameter_filters),
            include_archived=self._include_archived,
            local_metadata_filters=dict(self._local_metadata_filters),
            effective_metadata_filters=dict(self._effective_metadata_filters),
            on_unloaded=self._on_unloaded,
        )
        params.update(overrides)
        return self.__class__(**params)

    def include(self, relation_names: str | Sequence[str]) -> "ResourceQuery":
        if self._shape == "ref":
            raise ValueError("include(...) is only valid when shape='full'")
        if self._load == "eager":
            raise ValueError("include(...) cannot be combined with load='eager'")
        return super().include(relation_names)

    def include_template(self) -> "ResourceQuery":
        return self.include("template")

    def filter_property(
        self,
        name: str,
        *,
        group: str | None = None,
        eq: Any | None = None,
        gt: Any | None = None,
        gte: Any | None = None,
        lt: Any | None = None,
        lte: Any | None = None,
        between: tuple[Any, Any] | None = None,
        in_: Sequence[Any] | None = None,
        value_type: str | None = None,
    ) -> "ResourceQuery":
        comparators = {
            "eq": eq,
            "gt": gt,
            "gte": gte,
            "lt": lt,
            "lte": lte,
            "between": between,
            "in": in_,
        }
        set_ops = {op for op, v in comparators.items() if v is not None}
        if len(set_ops) != 1:
            raise ValueError(
                "filter_property requires exactly one comparator (eq/gt/gte/lt/lte/between/in_)"
            )
        op = next(iter(set_ops))
        raw_value = comparators[op]

        if op == "between":
            if not isinstance(raw_value, Sequence) or len(raw_value) != 2:
                raise ValueError(
                    "between requires a 2-tuple/sequence of (lower, upper)"
                )
            lower, upper = raw_value
        else:
            lower, upper = raw_value, None

        if value_type is None:
            probe = lower if op != "between" else lower
            value_type = self._infer_value_type(probe)

        pf = PropertyFilter(
            name=name,
            group=group,
            op=op,  # type: ignore[arg-type]
            value=lower,
            upper=upper,
            value_type=value_type,
        )
        return self._clone(property_filters=self._property_filters + [pf])

    def under_parent(
        self, parent: ResourceRef | ResourceSchema | UUID | str | Any
    ) -> "ResourceQuery":
        parent_id: UUID
        if isinstance(parent, ResourceRef | ResourceSchema):
            parent_id = parent.id
        elif isinstance(parent, UUID):
            parent_id = parent
        elif isinstance(parent, str):
            parent_id = UUID(parent)
        elif hasattr(parent, "id"):
            parent_id = parent.id
        else:
            raise TypeError(
                "parent must be a ResourceRef, ResourceSchema, UUID, UUID string, or have an 'id' attribute"
            )
        return self._clone(parent_resource_id=parent_id)

    def descendants(
        self,
        parent: ResourceRef | ResourceSchema | UUID | str | Any,
        *,
        of_template: UUID | None = None,
    ) -> "ResourceQuery":
        """Fetch every resource beneath ``parent`` (all levels) in one bulk
        recursive query, with ``template`` and ``properties`` eagerly loaded.

        Wraps the recommended ``under_parent(parent).include([...])`` pattern.
        Pass ``of_template`` to restrict to a single resource template.
        """
        q = self.under_parent(parent).include(["template", "properties"])
        if of_template is not None:
            q = q.filter(resource_template_id=of_template)
        return q


class ResourceTemplateQuery(BaseQuery[ResourceTemplateSchema | ResourceTemplateRef]):
    model = ResourceTemplateSchema
    default_schema = None

    def __init__(
        self,
        backend: "Backend",
        *,
        shape: ShapeInput = "full",
        load: LoadInput = "none",
        on_unloaded: OnUnloadedPolicy | None = None,
        **kwargs,
    ):
        shape = _normalize_shape(shape)
        load = _normalize_load(load)
        if shape == "ref" and load != "none":
            raise ValueError("load must be 'none' when shape='ref'")
        self._shape = shape
        self._load = load
        model = ResourceTemplateSchema if shape == "full" else ResourceTemplateRef
        super().__init__(
            backend,
            model=model,
            load_mode=load,
            on_unloaded=on_unloaded,
            **kwargs,
        )

    def _clone(self, **overrides) -> "ResourceTemplateQuery":
        params = dict(
            backend=self._backend,
            context=self._context,
            shape=self._shape,
            load=self._load,
            filters=dict(self._filters),
            predicates=list(self._predicates),
            orderings=list(self._orderings),
            preloads=list(self._preloads),
            limit=self._limit,
            offset=self._offset,
            property_filters=list(self._property_filters),
            parent_resource_id=self._parent_resource_id,
            parameter_filters=list(self._parameter_filters),
            include_archived=self._include_archived,
            local_metadata_filters=dict(self._local_metadata_filters),
            effective_metadata_filters=dict(self._effective_metadata_filters),
            on_unloaded=self._on_unloaded,
        )
        params.update(overrides)
        return self.__class__(**params)

    def include(self, relation_names: str | Sequence[str]) -> "ResourceTemplateQuery":
        if self._shape == "ref":
            raise ValueError("include(...) is only valid when shape='full'")
        if self._load == "eager":
            raise ValueError("include(...) cannot be combined with load='eager'")
        return super().include(relation_names)

    def filter_by_types(self, type_list: list[str]) -> "ResourceTemplateQuery":
        return self.filter(types__names_in=type_list)

    def filter_label(self, label: str) -> "ResourceTemplateQuery":
        return self.filter(labels__contains=label)

    def include_children(self) -> "ResourceTemplateQuery":
        return self.include("children")

    def include_attribute_groups(self) -> "ResourceTemplateQuery":
        return self.include("attribute_group_templates")

    def include_types(self) -> "ResourceTemplateQuery":
        return self.include("types")


class ProcessTemplateQuery(BaseQuery[ProcessTemplateSchema | ProcessTemplateRef]):
    model = ProcessTemplateSchema
    default_schema = None

    def __init__(
        self,
        backend: "Backend",
        *,
        shape: ShapeInput = "full",
        load: LoadInput = "none",
        on_unloaded: OnUnloadedPolicy | None = None,
        **kwargs,
    ):
        shape = _normalize_shape(shape)
        load = _normalize_load(load)
        if shape == "ref" and load != "none":
            raise ValueError("load must be 'none' when shape='ref'")
        self._shape = shape
        self._load = load
        model = ProcessTemplateSchema if shape == "full" else ProcessTemplateRef
        super().__init__(
            backend,
            model=model,
            load_mode=load,
            on_unloaded=on_unloaded,
            **kwargs,
        )

    def _clone(self, **overrides) -> "ProcessTemplateQuery":
        params = dict(
            backend=self._backend,
            context=self._context,
            shape=self._shape,
            load=self._load,
            filters=dict(self._filters),
            predicates=list(self._predicates),
            orderings=list(self._orderings),
            preloads=list(self._preloads),
            limit=self._limit,
            offset=self._offset,
            property_filters=list(self._property_filters),
            parent_resource_id=self._parent_resource_id,
            parameter_filters=list(self._parameter_filters),
            include_archived=self._include_archived,
            local_metadata_filters=dict(self._local_metadata_filters),
            effective_metadata_filters=dict(self._effective_metadata_filters),
            on_unloaded=self._on_unloaded,
        )
        params.update(overrides)
        return self.__class__(**params)

    def include(self, relation_names: str | Sequence[str]) -> "ProcessTemplateQuery":
        if self._shape == "ref":
            raise ValueError("include(...) is only valid when shape='full'")
        if self._load == "eager":
            raise ValueError("include(...) cannot be combined with load='eager'")
        return super().include(relation_names)

    def include_step_templates(self) -> "ProcessTemplateQuery":
        return self.include("step_templates")

    def include_resource_slots(self) -> "ProcessTemplateQuery":
        return self.include("resource_slots")

    def filter_label(self, label: str) -> "ProcessTemplateQuery":
        return self.filter(labels__contains=label)


class QueryDSL:
    def __init__(
        self,
        backend: "Backend",
        *,
        context: NamespaceContext,
        on_unloaded: OnUnloadedPolicy = "warn",
    ):
        if on_unloaded not in {"silent", "warn", "raise"}:
            raise ValueError("on_unloaded must be one of: 'silent', 'warn', 'raise'")
        self.backend = backend
        self.context = context
        self._on_unloaded = on_unloaded

    def namespaces(self) -> NamespaceQuery:
        return NamespaceQuery(self.backend, context=self.context)

    def process_runs(
        self,
        *,
        shape: ShapeInput = "full",
        load: LoadInput = "none",
        on_unloaded: OnUnloadedPolicy | None = None,
    ) -> ProcessRunQuery:
        return ProcessRunQuery(
            self.backend,
            context=self.context,
            shape=_normalize_shape(shape),
            load=_normalize_load(load),
            on_unloaded=self._on_unloaded if on_unloaded is None else on_unloaded,
        )

    def process_templates(
        self,
        *,
        shape: ShapeInput = "full",
        load: LoadInput = "none",
        on_unloaded: OnUnloadedPolicy | None = None,
    ) -> ProcessTemplateQuery:
        return ProcessTemplateQuery(
            self.backend,
            context=self.context,
            shape=_normalize_shape(shape),
            load=_normalize_load(load),
            on_unloaded=self._on_unloaded if on_unloaded is None else on_unloaded,
        )

    def resources(
        self,
        *,
        shape: ShapeInput = "full",
        load: LoadInput = "none",
        on_unloaded: OnUnloadedPolicy | None = None,
    ) -> ResourceQuery:
        return ResourceQuery(
            self.backend,
            context=self.context,
            shape=_normalize_shape(shape),
            load=_normalize_load(load),
            on_unloaded=self._on_unloaded if on_unloaded is None else on_unloaded,
        )

    def resource_templates(
        self,
        *,
        shape: ShapeInput = "full",
        load: LoadInput = "none",
        on_unloaded: OnUnloadedPolicy | None = None,
    ) -> ResourceTemplateQuery:
        return ResourceTemplateQuery(
            self.backend,
            context=self.context,
            shape=_normalize_shape(shape),
            load=_normalize_load(load),
            on_unloaded=self._on_unloaded if on_unloaded is None else on_unloaded,
        )
