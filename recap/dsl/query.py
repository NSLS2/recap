from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field

from recap.schemas.resource import (
    ResourceRef,
    ResourceSchema,
    ResourceTemplateRef,
    ResourceTemplateSchema,
)

if TYPE_CHECKING:
    from recap.adapter import Backend
from recap.schemas.process import (
    CampaignSchema,
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
    property_filters: list[PropertyFilter] = Field(default_factory=list)
    parent_resource_id: UUID | None = None
    parameter_filters: list[ParameterFilter] = Field(default_factory=list)
    campaign_id: UUID | None = None
    load_mode: Literal["none", "full"] | None = None
    on_unloaded: OnUnloadedPolicy | None = None


class BaseQuery(Generic[SchemaT]):
    schema: type[SchemaT]

    def __init__(
        self: Self,
        backend: "Backend",
        *,
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
        campaign_id: UUID | None = None,
        load_mode: Literal["none", "full"] | None = None,
        on_unloaded: OnUnloadedPolicy | None = None,
    ):
        self._backend = backend
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
        self._campaign_id = campaign_id
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
            campaign_id=self._campaign_id,
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
        return self._clone(predicates=self._predicates + list(predicates))

    def order_by(self, *orderings) -> "Self":
        return self._clone(orderings=self._orderings + list(orderings))

    def limit(self, value: int) -> "Self":
        return self._clone(limit=value)

    def offset(self, value: int) -> "Self":
        return self._clone(offset=value)

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
            campaign_id=self._campaign_id,
            load_mode=self._load_mode,
            on_unloaded=self._on_unloaded,
        )

    def _execute(self) -> list[SchemaT]:
        rows = self._backend.query(self.model, self._spec)
        return rows

    def all(self) -> Sequence[SchemaT] | Sequence[BaseModel]:
        return self._execute()

    def first(self) -> SchemaT | None:
        return next(iter(self.limit(1)._execute()), None)

    def count(self) -> int:
        return self._backend.count(self.model, self._spec)


class CampaignQuery(BaseQuery[CampaignSchema]):
    model = CampaignSchema
    default_schema = None  # e.g. recap.schemas.campaign.CampaignSchema

    def include_process_runs(
        self,
    ) -> "CampaignQuery":
        return self.include("process_run")


class ProcessRunQuery(BaseQuery[ProcessRunSchema | ProcessRunRef]):
    model = ProcessRunSchema
    default_schema = None

    def __init__(
        self,
        backend: "Backend",
        *,
        shape: Literal["schema", "ref"] = "schema",
        load: Literal["none", "full"] = "none",
        on_unloaded: OnUnloadedPolicy | None = None,
        **kwargs,
    ):
        if shape == "ref" and load != "none":
            raise ValueError("load must be 'none' when shape='ref'")
        self._shape = shape
        self._load = load
        model = ProcessRunSchema if shape == "schema" else ProcessRunRef
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
            campaign_id=self._campaign_id,
            on_unloaded=self._on_unloaded,
        )
        params.update(overrides)
        return self.__class__(**params)

    def include(self, relation_names: str | Sequence[str]) -> "ProcessRunQuery":
        if self._shape == "ref":
            raise ValueError("include(...) is only valid when shape='schema'")
        if self._load == "full":
            raise ValueError("include(...) cannot be combined with load='full'")
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
        shape: Literal["schema", "ref"] = "schema",
        load: Literal["none", "full"] = "none",
        on_unloaded: OnUnloadedPolicy | None = None,
        **kwargs,
    ):
        if shape == "ref" and load != "none":
            raise ValueError("load must be 'none' when shape='ref'")
        self._shape = shape
        self._load = load
        model = ResourceSchema if shape == "schema" else ResourceRef
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
            campaign_id=self._campaign_id,
            on_unloaded=self._on_unloaded,
        )
        params.update(overrides)
        return self.__class__(**params)

    def include(self, relation_names: str | Sequence[str]) -> "ResourceQuery":
        if self._shape == "ref":
            raise ValueError("include(...) is only valid when shape='schema'")
        if self._load == "full":
            raise ValueError("include(...) cannot be combined with load='full'")
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


class ResourceTemplateQuery(BaseQuery[ResourceTemplateSchema | ResourceTemplateRef]):
    model = ResourceTemplateSchema
    default_schema = None

    def __init__(
        self,
        backend: "Backend",
        *,
        shape: Literal["schema", "ref"] = "schema",
        load: Literal["none", "full"] = "none",
        on_unloaded: OnUnloadedPolicy | None = None,
        **kwargs,
    ):
        if shape == "ref" and load != "none":
            raise ValueError("load must be 'none' when shape='ref'")
        self._shape = shape
        self._load = load
        model = ResourceTemplateSchema if shape == "schema" else ResourceTemplateRef
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
            campaign_id=self._campaign_id,
            on_unloaded=self._on_unloaded,
        )
        params.update(overrides)
        return self.__class__(**params)

    def include(self, relation_names: str | Sequence[str]) -> "ResourceTemplateQuery":
        if self._shape == "ref":
            raise ValueError("include(...) is only valid when shape='schema'")
        if self._load == "full":
            raise ValueError("include(...) cannot be combined with load='full'")
        return super().include(relation_names)

    def filter_by_types(self, type_list: list[str]) -> "ResourceTemplateQuery":
        return self.filter(types__names_in=type_list)

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
        shape: Literal["schema", "ref"] = "schema",
        load: Literal["none", "full"] = "none",
        on_unloaded: OnUnloadedPolicy | None = None,
        **kwargs,
    ):
        if shape == "ref" and load != "none":
            raise ValueError("load must be 'none' when shape='ref'")
        self._shape = shape
        self._load = load
        model = ProcessTemplateSchema if shape == "schema" else ProcessTemplateRef
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
            campaign_id=self._campaign_id,
            on_unloaded=self._on_unloaded,
        )
        params.update(overrides)
        return self.__class__(**params)

    def include(self, relation_names: str | Sequence[str]) -> "ProcessTemplateQuery":
        if self._shape == "ref":
            raise ValueError("include(...) is only valid when shape='schema'")
        if self._load == "full":
            raise ValueError("include(...) cannot be combined with load='full'")
        return super().include(relation_names)

    def include_step_templates(self) -> "ProcessTemplateQuery":
        return self.include("step_templates")

    def include_resource_slots(self) -> "ProcessTemplateQuery":
        return self.include("resource_slots")


class QueryDSL:
    def __init__(
        self,
        backend: "Backend",
        *,
        campaign_id: UUID | None = None,
        on_unloaded: OnUnloadedPolicy = "warn",
    ):
        if on_unloaded not in {"silent", "warn", "raise"}:
            raise ValueError("on_unloaded must be one of: 'silent', 'warn', 'raise'")
        self.backend = backend
        self._campaign_id = campaign_id
        self._on_unloaded = on_unloaded

    def _resolve_campaign_id(self, campaign: UUID | str | Any | None) -> UUID | None:
        if campaign is None:
            return None
        if isinstance(campaign, UUID):
            return campaign
        if isinstance(campaign, str):
            return UUID(campaign)
        if hasattr(campaign, "id"):
            return campaign.id
        raise TypeError("campaign must be a UUID, UUID string, or object with an id")

    def _pick_campaign_id(self, campaign: UUID | str | Any | None) -> UUID | None:
        return (
            self._resolve_campaign_id(campaign)
            if campaign is not None
            else self._campaign_id
        )

    def campaigns(self) -> CampaignQuery:
        return CampaignQuery(self.backend)

    def process_runs(
        self,
        *,
        shape: Literal["schema", "ref"] = "schema",
        load: Literal["none", "full"] = "none",
        campaign: UUID | str | Any | None = None,
        on_unloaded: OnUnloadedPolicy | None = None,
    ) -> ProcessRunQuery:
        campaign_id = self._pick_campaign_id(campaign)
        return ProcessRunQuery(
            self.backend,
            shape=shape,
            load=load,
            campaign_id=campaign_id,
            on_unloaded=self._on_unloaded if on_unloaded is None else on_unloaded,
        )

    def process_templates(
        self,
        *,
        shape: Literal["schema", "ref"] = "schema",
        load: Literal["none", "full"] = "none",
        on_unloaded: OnUnloadedPolicy | None = None,
    ) -> ProcessTemplateQuery:
        return ProcessTemplateQuery(
            self.backend,
            shape=shape,
            load=load,
            on_unloaded=self._on_unloaded if on_unloaded is None else on_unloaded,
        )

    def resources(
        self,
        *,
        shape: Literal["schema", "ref"] = "schema",
        load: Literal["none", "full"] = "none",
        campaign: UUID | str | Any | None = None,
        on_unloaded: OnUnloadedPolicy | None = None,
    ) -> ResourceQuery:
        """Start a resource query, optionally scoped to a campaign.

        .. note:: Campaign scoping for resources

           Resources do not have a direct ``campaign_id`` column.  When a
           campaign is active, the scope is applied by joining through
           ``ResourceAssignment`` -> ``ProcessRun`` and filtering on
           ``ProcessRun.campaign_id``.  Resources that have never been
           assigned to a process run in the target campaign will be
           excluded.  Use ``query_maker(unscoped=True)`` to include all
           resources regardless of assignment status.
        """
        campaign_id = self._pick_campaign_id(campaign)
        return ResourceQuery(
            self.backend,
            shape=shape,
            load=load,
            campaign_id=campaign_id,
            on_unloaded=self._on_unloaded if on_unloaded is None else on_unloaded,
        )

    def resource_templates(
        self,
        *,
        shape: Literal["schema", "ref"] = "schema",
        load: Literal["none", "full"] = "none",
        on_unloaded: OnUnloadedPolicy | None = None,
    ) -> ResourceTemplateQuery:
        return ResourceTemplateQuery(
            self.backend,
            shape=shape,
            load=load,
            on_unloaded=self._on_unloaded if on_unloaded is None else on_unloaded,
        )
