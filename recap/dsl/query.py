from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from recap.adapter import Backend
from recap.schemas.process import (
    CampaignSchema,
    ProcessRunSchema,
    ResourceSchema,
    ResourceTemplateSchema,
)

try:
    from typing import Self
except ModuleNotFoundError:
    from typing import Self


SchemaT = TypeVar("SchemaT", bound=BaseModel)
ModelT = TypeVar("ModelT")


class QuerySpec(BaseModel):
    filters: dict[str, Any] = {}
    predicates: Sequence[Any] = ()
    orderings: Sequence[Any] = ()
    preloads: Sequence[str] = ()
    limit: int | None = None
    offset: int | None = None


class BaseQuery(Generic[SchemaT]):
    schema: type[SchemaT]

    def __init__(
        self: Self,
        backend: "Backend",
        *,
        filters: dict[str, Any] | None = None,
        predicates: list[Any] | None = None,
        orderings: list[Any] | None = None,
        preloads: list[str] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ):
        self._backend = backend
        self._filters = filters or {}
        self._predicates = predicates or []
        self._orderings = orderings or []
        self._preloads = preloads or []
        self._limit = limit
        self._offset = offset

    def _clone(self: Self, **overrides) -> "Self":
        """
        We can users to query data, not modify the object
        So for now we clone the sqlalchemy object and pass it
        to the user
        """
        params = dict(
            backend=self._backend,
            filters=dict(self._filters),
            predicates=list(self._predicates),
            orderings=list(self._orderings),
            preloads=list(self._preloads),
            limit=self._limit,
            offset=self._offset,
        )
        params.update(overrides)
        clone = self.__class__(**params)
        # clone._limit = self._limit
        # clone._offset = self._offset
        return clone

    def filter(self, **kwargs) -> "Self":
        # stmt = self._statement.filter_by(**kwargs)
        new_filters = dict(self._filters)
        new_filters.update(kwargs)
        return self._clone(filters=new_filters)

    def where(self, *predicates) -> "Self":
        # stmt = self._statement.where(*predicates)
        return self._clone(predicates=self._predicates + list(predicates))

    def order_by(self, *orderings) -> "Self":
        # stmt = self._statement.order_by(*orderings)
        return self._clone(orderings=self._orderings + list(orderings))

    def limit(self, value: int) -> "Self":
        # clone = self._clone()
        # clone._limit = value
        return self._clone(limit=value)

    def offset(self, value: int) -> "Self":
        # clone = self._clone()
        # clone._offset = value
        return self._clone(offset=value)

    def include(self, relation_name) -> "Self":
        # clone = self._clone()
        # clone._preloads.append(loader)
        return self._clone(preloads=self._preloads + [relation_name])

    @property
    def _spec(self) -> QuerySpec:
        return QuerySpec(
            filters=self._filters,
            predicates=self._predicates,
            orderings=self._orderings,
            preloads=self._preloads,
            limit=self._limit,
            offset=self._offset,
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


class ProcessRunQuery(BaseQuery[ProcessRunSchema]):
    model = ProcessRunSchema
    default_schema = None

    def include_steps(self, *, include_parameters: bool = False) -> "ProcessRunQuery":
        if include_parameters:
            self.include("steps.parameters")
        return self.include("steps")

    def include_resources(self) -> "ProcessRunQuery":
        return self.include("resources")


class ResourceQuery(BaseQuery[ResourceSchema]):
    model = ResourceSchema
    default_schema = None

    def include_template(self) -> "ResourceQuery":
        return self.include("template")


class ResourceTemplateQuery(BaseQuery[ResourceTemplateSchema]):
    model = ResourceTemplateSchema
    default_schema = None

    def filter_by_types(self, type_list: list[str]) -> "ResourceTemplateQuery":
        return self.filter(types__names_in=type_list)


class QueryDSL:
    def __init__(self, backend: "Backend"):
        self.backend = backend

    def campaigns(self) -> CampaignQuery:
        return CampaignQuery(self.backend)

    def process_runs(self) -> ProcessRunQuery:
        return ProcessRunQuery(self.backend)

    def resources(self) -> ResourceQuery:
        return ResourceQuery(self.backend)

    def resource_templates(self) -> ResourceTemplateQuery:
        return ResourceTemplateQuery(self.backend)
