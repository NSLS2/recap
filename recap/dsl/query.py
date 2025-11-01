from collections.abc import Callable, Sequence
from contextlib import contextmanager
from typing import Any, Generic, TypeVar

from pydantic import BaseModel
from sqlalchemy import Select, select

from recap.schemas.process import ProcessRunSchema, ResourceTemplateSchema

try:
    from typing import Self
except ModuleNotFoundError:
    from typing import Self

from sqlalchemy.orm import selectinload

from recap.db.campaign import Campaign
from recap.db.process import ProcessRun, ResourceAssignment
from recap.db.resource import Resource, ResourceTemplate, ResourceType
from recap.db.step import Parameter, Step

ModelT = TypeVar("ModelT")


class BaseQuery(Generic[ModelT]):
    model: type[ModelT]
    schema_factory: Callable[[Sequence[ModelT]], Sequence[BaseModel]]

    def __init__(
        self: Self,
        session_scope,
        statement: Select | None = None,
        preloads: list[Any] | None = None,
    ):
        self._session_scope = session_scope
        self._statement: Select = (
            statement if statement is not None else select(self.model)
        )
        self._preloads = preloads or []
        self._limit: int | None = None
        self._offset: int | None = None

    def _clone(self: Self, **overrides) -> "Self":
        """
        We can users to query data, not modify the object
        So for now we clone the sqlalchemy object and pass it
        to the user
        """
        params = {
            "session_scope": self._session_scope,
            "statement": self._statement,
            "preloads": list(self._preloads),
        }
        params.update(overrides)
        clone = self.__class__(**params)
        clone._limit = self._limit
        clone._offset = self._offset
        return clone

    def filter(self, **kwargs) -> "Self":
        stmt = self._statement.filter_by(**kwargs)
        return self._clone(statement=stmt)

    def where(self, *predicates) -> "Self":
        stmt = self._statement.where(*predicates)
        return self._clone(statement=stmt)

    def order_by(self, *orderings) -> "Self":
        stmt = self._statement.order_by(*orderings)
        return self._clone(statement=stmt)

    def limit(self, value: int) -> "Self":
        clone = self._clone()
        clone._limit = value
        return clone

    def offset(self, value: int) -> "Self":
        clone = self._clone()
        clone._offset = value
        return clone

    def include(self, loader) -> "Self":
        clone = self._clone()
        clone._preloads.append(loader)
        return clone

    def _apply_window(self, stmt: Select) -> Select:
        if self._limit is not None:
            stmt = stmt.limit(self._limit)
        if self._offset is not None:
            stmt = stmt.offset(self._offset)
        # for loader in self._preloads:
        if self._preloads:
            stmt = stmt.options(*self._preloads)
        return stmt

    def _execute(self, schema_transformer=None) -> list[ModelT]:
        with self._session_scope() as session:
            stmt = self._apply_window(self._statement)
            rows = list(session.scalars(stmt).unique())
            if schema_transformer:
                return schema_transformer(rows)
            return rows

    def all(self) -> Sequence[ModelT] | Sequence[BaseModel]:
        # return self._to_schema(self._execute())
        if not self.schema_factory:
            return self._execute()
        return self._execute(self.schema_factory)

    def _to_schema(self, rows) -> Sequence[ModelT] | Sequence[BaseModel]:
        if not self.schema_factory:
            return rows
        return self.schema_factory(rows)

    def first(self) -> ModelT | None:
        return next(iter(self.limit(1)._execute()), None)

    def count(self) -> int:
        with self._session_scope() as session:
            return session.execute(
                self._statement.with_only_columns(self.model.id).count()
            )

    def as_models(self) -> Sequence[ModelT]:
        return self._execute()


class CampaignQuery(BaseQuery[Campaign]):
    model = Campaign
    default_schema = None  # e.g. recap.schemas.campaign.CampaignSchema

    def include_process_runs(
        self,
        configurator: Callable[["ProcessRunQuery"], "ProcessRunQuery"] | None = None,
    ) -> "CampaignQuery":
        loader = selectinload(Campaign.process_runs)
        if configurator:
            pr_query = configurator(ProcessRunQuery(self._session_scope))
            for option in pr_query._preloads:
                loader = loader.options(option)
        return self.include(loader)


class ProcessRunQuery(BaseQuery[ProcessRun]):
    model = ProcessRun
    default_schema = None

    def schema_factory(self, rows):
        return [ProcessRunSchema.model_validate(r) for r in rows]

    def include_steps(self, *, include_parameters: bool = False) -> "ProcessRunQuery":
        loader = selectinload(ProcessRun.steps)
        if include_parameters:
            loader = loader.options(
                selectinload(Step.parameters).selectinload(
                    Parameter._values
                )  # e.g., to pull AttributeValue rows
            )
        return self.include(loader)

    def include_resources(self) -> "ProcessRunQuery":
        loaders = selectinload(ProcessRun.assignments).selectinload(
            ResourceAssignment.resource
        )
        return self.include(loaders)


class ResourceQuery(BaseQuery[Resource]):
    model = Resource
    default_schema = None

    def include_template(self) -> "ResourceQuery":
        return self.include(selectinload(Resource.template))


class ResourceTemplateQuery(BaseQuery[ResourceTemplate]):
    model = ResourceTemplate
    default_schema = None

    def schema_factory(self, rows):
        return [ResourceTemplateSchema.model_validate(r) for r in rows]

    def filter_by_types(self, type_list: list[str]) -> "ResourceTemplateQuery":
        stmt = (
            self._statement.join(ResourceTemplate.types)
            .where(ResourceType.name.in_(type_list))
            .group_by(ResourceTemplate.id)
        )
        return self._clone(statement=stmt)


class QueryDSL:
    def __init__(self, session_factory):
        self._session_factory = session_factory

    @contextmanager
    def _session_scope(self):
        session = self._session_factory()
        try:
            yield session
        finally:
            session.close()

    def campaigns(self) -> CampaignQuery:
        return CampaignQuery(self._session_scope)

    def process_runs(self) -> ProcessRunQuery:
        return ProcessRunQuery(self._session_scope)

    def resources(self) -> ResourceQuery:
        return ResourceQuery(self._session_scope)

    def resource_templates(self) -> ResourceTemplateQuery:
        return ResourceTemplateQuery(self._session_scope)
