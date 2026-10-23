import json
from collections.abc import Sequence
from contextlib import contextmanager
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, TypeAdapter, create_model
from pydantic import ValidationError as PydanticError
from sqlalchemy import (
    Float,
    Integer,
    Select,
    String,
    cast,
    exists,
    func,
    select,
)
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql import and_, or_
from sqlalchemy.sql.functions import count

from recap.adapter import (
    NamespaceCatalog,
    NamespaceContextResolver,
    NamespaceWriter,
    ReadBackend,
    WriteBackend,
)
from recap.adapter.entity_batch import EntityBatch
from recap.adapter.entity_loading import load_resource_subtrees, prepare_batch_graph
from recap.adapter.query_loaders import (
    preload_options,
    resolve_loader_options,
)
from recap.adapter.schema_registry import SCHEMA_REGISTRY
from recap.authorization.query import AuthorizedQuery
from recap.commands.models import (
    CommandContext,
    CommandModel,
    CreateNamespace,
    UpdateNamespace,
)
from recap.commands.service import CommandService
from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate, AttributeValue
from recap.db.base import Base
from recap.db.namespace import Namespace, NamespaceRepository
from recap.db.process import (
    ProcessRun,
    ProcessTemplate,
)
from recap.db.resource import (
    Property,
    Resource,
    ResourceTemplate,
    ResourceType,
)
from recap.db.step import Parameter, Step
from recap.dsl.query import FieldOrdering, FieldPredicate, QuerySpec, SchemaT
from recap.exceptions import (
    AuthorizationDenied,
    RecapNotFoundError,
    RecapPermissionDeniedError,
)
from recap.lifecycle import LifecycleStatus
from recap.schemas.namespace import NamespaceContext, NamespaceRef, NamespaceSchema
from recap.schemas.process import (
    ProcessRunRef,
    ProcessRunSchema,
    ProcessTemplateRef,
    ProcessTemplateSchema,
)
from recap.schemas.resource import (
    ResourceRef,
    ResourceSchema,
    ResourceTemplateRef,
    ResourceTemplateSchema,
)
from recap.schemas.step import (
    StepSchema,
)
from recap.utils.database import load_single
from recap.utils.dsl import AliasMixin, build_param_values_model, resolve_path
from recap.utils.general import make_slug, to_json_compatible
from recap.utils.namespace import namespace_ancestors

SCHEMA_MODEL_MAPPING: dict[type[BaseModel], type[Base]] = {
    NamespaceSchema: Namespace,
    NamespaceRef: Namespace,
    ResourceTemplateSchema: ResourceTemplate,
    ResourceTemplateRef: ResourceTemplate,
    ProcessRunRef: ProcessRun,
    ProcessRunSchema: ProcessRun,
    ProcessTemplateRef: ProcessTemplate,
    ResourceSchema: Resource,
    ResourceRef: Resource,
    ProcessTemplateSchema: ProcessTemplate,
}


def _coerce_field_value(field_name: str, column, value):
    try:
        python_type = column.property.columns[0].type.python_type
        return TypeAdapter(python_type).validate_python(value)
    except (PydanticError, TypeError, ValueError) as err:
        raise ValueError(f"Invalid value {value!r} for field '{field_name}'") from err


def _translate_field_predicate(column, predicate: FieldPredicate):
    string_operations = {"contains", "starts_with", "ends_with"}
    python_type = column.property.columns[0].type.python_type

    if predicate.op in string_operations:
        if python_type is not str or not isinstance(predicate.value, str):
            raise ValueError(
                f"Field '{predicate.field}' requires a string column and string value "
                f"for operator '{predicate.op}'"
            )
        value = predicate.value
    elif predicate.op in {"in", "not_in"}:
        if isinstance(predicate.value, str) or not isinstance(
            predicate.value, Sequence
        ):
            raise ValueError(
                f"Invalid membership value for field '{predicate.field}': "
                "expected a non-string sequence"
            )
        value = [
            _coerce_field_value(predicate.field, column, item)
            for item in predicate.value
        ]
    elif predicate.value is None and predicate.op in {"eq", "ne"}:
        value = None
    else:
        value = _coerce_field_value(predicate.field, column, predicate.value)

    operations = {
        "eq": lambda column, value: column == value,
        "ne": lambda column, value: column != value,
        "gt": lambda column, value: column > value,
        "gte": lambda column, value: column >= value,
        "lt": lambda column, value: column < value,
        "lte": lambda column, value: column <= value,
        "in": lambda column, value: column.in_(value),
        "not_in": lambda column, value: column.not_in(value),
        "contains": lambda column, value: column.contains(value, autoescape=True),
        "starts_with": lambda column, value: column.startswith(value, autoescape=True),
        "ends_with": lambda column, value: column.endswith(value, autoescape=True),
    }
    return operations[predicate.op](column, value)


def _apply_field_expressions(model, stmt, spec, joined_paths):
    for predicate in spec.predicates:
        if isinstance(predicate, FieldPredicate):
            stmt, column = resolve_path(
                model, stmt, tuple(predicate.field.split(".")), joined_paths
            )
            predicate = _translate_field_predicate(column, predicate)
        stmt = stmt.where(predicate)

    orderings = []
    for ordering in spec.orderings:
        if isinstance(ordering, FieldOrdering):
            stmt, column = resolve_path(
                model, stmt, tuple(ordering.field.split(".")), joined_paths
            )
            ordering = column.asc() if ordering.direction == "asc" else column.desc()
        orderings.append(ordering)
    if orderings:
        stmt = stmt.order_by(*orderings)
    return stmt


_SPECIAL_FILTERS = {"labels__contains", "types__names_in"}


def _validate_query_paths(schema: type[BaseModel], spec: QuerySpec) -> None:
    try:
        model = SCHEMA_REGISTRY.by_model(schema).orm_model
    except KeyError:
        model = SCHEMA_MODEL_MAPPING[schema]
    stmt = select(model)
    joined_paths: dict[tuple[str, ...], type] = {}

    for field in spec.filters:
        if field in _SPECIAL_FILTERS:
            continue
        resolve_path(model, stmt, tuple(field.split("__")), joined_paths)

    for predicate in spec.predicates:
        if not isinstance(predicate, FieldPredicate):
            raise ValueError("Query predicates must use Field(...)")
        resolve_path(model, stmt, tuple(predicate.field.split(".")), joined_paths)

    for ordering in spec.orderings:
        if not isinstance(ordering, FieldOrdering):
            raise ValueError("Query orderings must use Field(...)")
        resolve_path(model, stmt, tuple(ordering.field.split(".")), joined_paths)


class LocalBackend(
    ReadBackend,
    WriteBackend,
    NamespaceCatalog,
    NamespaceContextResolver,
    NamespaceWriter,
):
    def __init__(self, session_factory):
        self._session_factory = session_factory

    def close(self):
        """Retained for client lifecycle compatibility; sessions are short-lived."""
        return None

    def validate_query(self, schema: type[SchemaT], spec: QuerySpec) -> None:
        _validate_query_paths(schema, spec)

    def execute(
        self,
        command: CommandModel,
        context: CommandContext,
        *,
        etag_override: str | None = None,
    ) -> object:
        result = self.execute_batch(command, context, etag_override=etag_override)
        return CommandService._model_result(result)

    def execute_batch(self, command, context, *, etag_override=None):
        return self._execute_command(command, context)

    def _execute_command(self, command, context):
        try:
            service = CommandService(self._session_factory)
            return service.execute_batch(command, context)
        except AuthorizationDenied as error:
            if error.conceal:
                raise RecapNotFoundError("Not found") from None
            raise RecapPermissionDeniedError("Permission denied") from None

    @contextmanager
    def _session_scope(self):
        """Yield one short-lived read session."""
        session = self._session_factory()
        try:
            yield session
        finally:
            session.close()

    def create_namespace(
        self,
        path: str,
        metadata: dict[str, Any] | None,
        context: CommandContext,
    ) -> NamespaceContext:
        result = self.execute(CreateNamespace(path=path, metadata=metadata), context)
        return self._namespace_context(result)

    def update_namespace(
        self,
        namespace_id: UUID,
        expected_revision: int,
        metadata: dict[str, Any] | None,
        status: LifecycleStatus | None,
        context: CommandContext,
        *,
        etag: str | None = None,
    ) -> NamespaceContext:
        result = self.execute(
            UpdateNamespace(
                namespace_id=namespace_id,
                expected_revision=expected_revision,
                metadata=metadata,
                status=status,
            ),
            context,
        )
        return self._namespace_context(result)

    @staticmethod
    def _namespace_context(
        namespace: NamespaceContext | NamespaceSchema,
    ) -> NamespaceContext:
        if isinstance(namespace, NamespaceContext):
            return namespace
        return NamespaceContext(
            id=namespace.id,
            path=namespace.path,
            metadata=namespace.metadata,
            status=namespace.status,
            revision=namespace.revision,
            etag=None,
        )

    def get_process_template(
        self,
        namespace_id: UUID,
        name: str | None,
        version: str | None,
        expand: bool = False,
        id: UUID | str | None = None,
    ) -> ProcessTemplateRef | ProcessTemplateSchema:
        statement = select(ProcessTemplate).where(
            ProcessTemplate.namespace_id == namespace_id
        )
        if name:
            statement = statement.where(ProcessTemplate.name == name)
        if version:
            statement = statement.where(ProcessTemplate.version == version)
        if isinstance(id, str):
            id = UUID(id)
        if id:
            statement = statement.where(ProcessTemplate.id == id)
        if not name and not id:
            raise ValueError("name or id required to fetch ProcessTemplate")
        spec = QuerySpec(load_mode="eager" if expand else "none")
        with self._session_scope() as session:
            process_template = load_single(session, statement, label="ProcessTemplate")
            batch = self._collect_query_rows(
                session, [process_template], ProcessTemplateSchema, spec
            )
        return self._materialize_batch(ProcessTemplateSchema, batch)[0]

    def get_resource_template(
        self,
        namespace_id: UUID,
        name: str | None,
        version: str | None = None,
        id: UUID | str | None = None,
        parent: ResourceTemplateRef | ResourceTemplate | None = None,
        expand: bool = False,
    ) -> ResourceTemplateRef | ResourceTemplateSchema:
        statement = select(ResourceTemplate).where(
            ResourceTemplate.namespace_id == namespace_id
        )
        if name:
            statement = statement.where(ResourceTemplate.name == name)
        if version:
            statement = statement.where(ResourceTemplate.version == version)
        if not name and not id:
            raise ValueError("name or id required to fetch ResourceTemplate")
        if isinstance(id, str):
            id = UUID(id)
        if id:
            statement = statement.where(ResourceTemplate.id == id)
        if parent:
            statement = statement.where(ResourceTemplate.parent_id == parent.id)
        spec = QuerySpec(load_mode="eager" if expand else "none")
        with self._session_scope() as session:
            template = load_single(session, statement, label="ResourceTemplate")
            batch = self._collect_query_rows(
                session, [template], ResourceTemplateSchema, spec
            )
        return self._materialize_batch(ResourceTemplateSchema, batch)[0]

    def find_resources_by_identity(
        self,
        namespace_id: UUID,
        name: str,
        parent_id: UUID | None,
        template_id: UUID,
    ) -> list[Resource]:
        """Lookup existing resources by the policy key (name, parent_id,
        resource_template_id).  Returns results in deterministic order
        (create_date, then id).
        """
        if parent_id is None:
            parent_clause = Resource.parent_id.is_(None)
        else:
            parent_clause = Resource.parent_id == parent_id
        stmt = (
            select(Resource)
            .where(
                parent_clause,
                Resource.namespace_id == namespace_id,
                Resource.name == name,
                Resource.resource_template_id == template_id,
            )
            .order_by(Resource.create_date, Resource.id)
        )
        with self._session_scope() as session:
            return list(session.scalars(stmt).all())

    def get_resource(
        self,
        namespace_id: UUID,
        name: str,
        template_name: str,
        template_version: str | None = "1.0",
        expand: bool = False,
    ) -> ResourceRef | ResourceSchema:
        stmt = (
            select(Resource)
            .join(Resource.template)
            .where(
                Resource.name == name,
                Resource.namespace_id == namespace_id,
                ResourceTemplate.name == template_name,
                ResourceTemplate.version == template_version,
            )
        )
        spec = QuerySpec(load_mode="eager" if expand else "none")
        with self._session_scope() as session:
            resource = load_single(session, stmt, label="Resource")
            batch = self._collect_query_rows(session, [resource], ResourceSchema, spec)
        return self._materialize_batch(ResourceSchema, batch)[0]

    def get_steps(self, process_run: ProcessRunSchema) -> list[StepSchema]:
        statement = select(Step).where(Step.process_run_id == process_run.id)
        with self._session_scope() as session:
            steps = list(session.scalars(statement).unique())
            batch = prepare_batch_graph(
                session,
                steps,
                StepSchema,
                {
                    "template",
                    "children",
                    "parameters",
                    "resources",
                    "resource_overrides",
                },
            )
        return self._materialize_batch(StepSchema, batch)

    def get_params(self, step_schema: StepSchema) -> type[BaseModel]:
        statement = select(Step).where(
            Step.id == step_schema.id,
        )
        with self._session_scope() as session:
            step: Step | None = session.scalars(statement).one_or_none()
            if step is None:
                raise RecapNotFoundError(f"Step not found: {step_schema.name}")
            batch = prepare_batch_graph(session, [step], StepSchema, {"parameters"})
        materialized = self._materialize_batch(StepSchema, batch)[0]
        params: dict[str, tuple] = {
            "step_name": (
                Literal[f"{step_schema.name}"],
                Field(default=step_schema.name),
            ),
            "step_id": (UUID, Field(default=materialized.id)),
        }
        groups = materialized.parameters
        for param in groups.values():
            template_key = tuple(
                (
                    value_template.name,
                    value_template.slug,
                    value_template.value_type,
                    value_template.metadata,
                    value_template.unit,
                )
                for value_template in param.template.attribute_templates
            )
            values_model = build_param_values_model(param.template.slug, template_key)
            raw_values = {
                field.alias or name: {
                    "value": value.value,
                    "unit": value.unit,
                }
                for name, field in type(param.values).model_fields.items()
                if (value := param.values.__dict__.get(name)) is not None
            }
            params[param.template.slug] = (
                values_model,
                Field(
                    default_factory=lambda vm=values_model,
                    values=raw_values: vm.model_validate(values),
                    alias=param.template.name,
                ),
            )
        model = create_model(
            f"{step_schema.name}", **params, __base__=(AliasMixin, BaseModel)
        )
        return model()

    def _coerce_value(self, pf, left_expr):
        # For int/float, SQLite stores JSON numbers as native numeric types,
        # so casting the column expression lets SQL comparison operators work
        # naturally.  For bool/str/enum, SQLite stores these as text
        # ('true'/'false' or '"value"'), so we skip the cast and instead
        # JSON-encode the comparison value to match the stored representation.
        type_mapping = {
            "int": Integer,
            "float": Float,
            "datetime": String,
        }
        sa_type = type_mapping.get(pf.value_type)
        if sa_type:
            left_expr = cast(left_expr, sa_type)

        coerced_value = pf.value
        if pf.value_type:
            coerced_value = to_json_compatible(pf.value_type, pf.value)
        # JSON columns store strings/enums/bools with JSON encoding
        # (e.g., '"active"', 'true').  Wrap with json.dumps so the SQL
        # comparison matches the stored representation.
        if pf.value_type in ("str", "enum"):
            if isinstance(coerced_value, str):
                coerced_value = json.dumps(coerced_value)
        elif pf.value_type == "bool":
            coerced_value = json.dumps(coerced_value)
        return left_expr, coerced_value

    def _between_filter(self, prop_filter, conditions, left_expr):
        if prop_filter.upper is None:
            raise ValueError("between comparator requires upper bound")
        lower = to_json_compatible(prop_filter.value_type, prop_filter.value)
        upper = to_json_compatible(prop_filter.value_type, prop_filter.upper)
        if prop_filter.value_type in ("str", "enum"):
            if isinstance(lower, str):
                lower = json.dumps(lower)
            if isinstance(upper, str):
                upper = json.dumps(upper)
        elif prop_filter.value_type == "bool":
            lower = json.dumps(lower)
            upper = json.dumps(upper)
        conditions.append(left_expr.between(lower, upper))

    def _in_filter(self, prop_filter, conditions, left_expr):
        if prop_filter.value is None:
            raise ValueError("in comparator requires an iterable of values")
        values = prop_filter.value
        if not isinstance(values, list | tuple | set):
            raise ValueError("in comparator requires an iterable of values")
        coerced = [to_json_compatible(prop_filter.value_type, v) for v in values]
        if prop_filter.value_type in ("str", "enum"):
            coerced = [json.dumps(v) if isinstance(v, str) else v for v in coerced]
        elif prop_filter.value_type == "bool":
            coerced = [json.dumps(v) for v in coerced]
        conditions.append(left_expr.in_(coerced))

    def _add_filters(self, pf, conditions, left_expr, coerced_value):
        match pf.op:
            case "eq":
                conditions.append(left_expr == coerced_value)
            case "gt":
                conditions.append(left_expr > coerced_value)
            case "gte":
                conditions.append(left_expr >= coerced_value)
            case "lt":
                conditions.append(left_expr < coerced_value)
            case "lte":
                conditions.append(left_expr <= coerced_value)
            case "between":
                self._between_filter(pf, conditions, left_expr)
            case "in":
                self._in_filter(pf, conditions, left_expr)
            case _:
                raise ValueError(f"Unsupported property comparator {pf.op}")

        return conditions

    def _build_conditions_attributes(
        self,
        pf,
        tmpl_alias: type[AttributeTemplate],
        val_alias: type[AttributeValue],
        group_alias: type[AttributeGroupTemplate] | None,
    ) -> list:
        conditions = []
        name_slug = make_slug(pf.name)
        conditions.append(or_(tmpl_alias.name == pf.name, tmpl_alias.slug == name_slug))

        if group_alias is not None and pf.group:
            group_slug = make_slug(pf.group)
            conditions.append(
                or_(group_alias.name == pf.group, group_alias.slug == group_slug)
            )

        if pf.value_type:
            conditions.append(tmpl_alias.value_type == pf.value_type)

        left_expr = val_alias.value_json
        left_expr, coerced_value = self._coerce_value(pf, left_expr)
        conditions = self._add_filters(pf, conditions, left_expr, coerced_value)

        return conditions

    def _build_select_resource(self, model, stmt, spec):
        if spec.parent_resource_id:
            res_tbl = Resource.__table__
            root_id = spec.parent_resource_id
            base_cte = (
                select(res_tbl.c.id).where(res_tbl.c.id == root_id).cte(recursive=True)
            )
            children = select(res_tbl.c.id).where(res_tbl.c.parent_id == base_cte.c.id)
            descendants_cte = base_cte.union_all(children)
            stmt = stmt.join(descendants_cte, model.id == descendants_cte.c.id)
            stmt = stmt.where(model.id != root_id)

        if spec.property_filters:
            for idx, pf in enumerate(spec.property_filters):
                prop_alias = aliased(Property, name=f"prop_filter_{idx}")
                val_alias = aliased(AttributeValue, name=f"prop_val_filter_{idx}")
                tmpl_alias = aliased(AttributeTemplate, name=f"prop_tmpl_filter_{idx}")
                group_alias = (
                    aliased(AttributeGroupTemplate, name=f"prop_group_filter_{idx}")
                    if pf.group
                    else None
                )

                stmt = stmt.join(prop_alias, prop_alias.resource_id == model.id)
                stmt = stmt.join(val_alias, val_alias.property_id == prop_alias.id)
                stmt = stmt.join(
                    tmpl_alias, tmpl_alias.id == val_alias.attribute_template_id
                )
                if group_alias is not None:
                    stmt = stmt.join(
                        group_alias,
                        group_alias.id == prop_alias.attribute_group_template_id,
                    )
                conditions = self._build_conditions_attributes(
                    pf, tmpl_alias, val_alias, group_alias
                )
                stmt = stmt.where(and_(*conditions))

            stmt = stmt.distinct()
        return stmt

    def _build_select_process_run(self, model, stmt, spec):
        if spec.parameter_filters:
            for idx, pf in enumerate(spec.parameter_filters):
                step_alias = aliased(Step, name=f"param_step_filter_{idx}")
                param_alias = aliased(Parameter, name=f"param_filter_{idx}")
                val_alias = aliased(AttributeValue, name=f"param_val_filter_{idx}")
                tmpl_alias = aliased(AttributeTemplate, name=f"param_tmpl_filter_{idx}")
                group_alias = (
                    aliased(AttributeGroupTemplate, name=f"param_group_filter_{idx}")
                    if pf.group
                    else None
                )

                stmt = stmt.join(step_alias, step_alias.process_run_id == model.id)
                stmt = stmt.join(param_alias, param_alias.step_id == step_alias.id)
                stmt = stmt.join(val_alias, val_alias.parameter_id == param_alias.id)
                stmt = stmt.join(
                    tmpl_alias, tmpl_alias.id == val_alias.attribute_template_id
                )
                if group_alias is not None:
                    stmt = stmt.join(
                        group_alias,
                        group_alias.id == param_alias.attribute_group_template_id,
                    )

                conditions = self._build_conditions_attributes(
                    pf, tmpl_alias, val_alias, group_alias
                )
                stmt = stmt.where(and_(*conditions))
            stmt = stmt.distinct()
        return stmt

    def _load_resource_subtrees(
        self, session: Session, root_ids: list[UUID]
    ) -> list[Resource]:
        """Compatibility entry point for attached full resource subtree reads."""
        return load_resource_subtrees(
            session, root_ids, {"template", "parent", "children", "properties"}
        )

    def _namespace_visibility(self, namespace_path: str) -> tuple[UUID, list[UUID]]:
        paths = namespace_ancestors(namespace_path)
        with self._session_scope() as session:
            by_path = {
                namespace.path: namespace.id
                for namespace in session.scalars(
                    select(Namespace).where(Namespace.path.in_(paths))
                )
            }
        try:
            context_id = by_path[namespace_path]
        except KeyError as exc:
            raise RecapNotFoundError(
                f"Namespace does not exist: {namespace_path}"
            ) from exc
        return context_id, [by_path[path] for path in paths if path in by_path]

    def get_namespace_path(self, namespace_id: UUID) -> str:
        with self._session_scope() as session:
            namespace = session.get(Namespace, namespace_id)
            if namespace is None:
                raise RecapNotFoundError(f"Namespace does not exist: {namespace_id}")
            return namespace.path

    def get_namespace_context(self, path: str) -> NamespaceContext:
        with self._session_scope() as session:
            namespace = session.scalars(
                select(Namespace).where(Namespace.path == path)
            ).one_or_none()
            if namespace is None:
                raise RecapNotFoundError(f"Namespace does not exist: {path}")
            return NamespaceContext.model_validate(namespace)

    def list_child_namespace_paths(self, parent_path: str) -> list[str]:
        """Return full paths for direct children of a namespace path."""
        with self._session_scope() as session:
            parent = session.scalars(
                select(Namespace).where(Namespace.path == parent_path)
            ).one_or_none()
            if parent is None:
                return []
            statement = select(Namespace).where(Namespace.parent_id == parent.id)
            return [namespace.path for namespace in session.scalars(statement)]

    def list_child_namespaces(self, parent_path: str) -> list[str]:
        prefix = f"{parent_path.strip('/')}/" if parent_path.strip("/") else ""
        return [
            path.removeprefix(prefix)
            for path in self.list_child_namespace_paths(parent_path)
        ]

    def _apply_namespace_visibility(
        self, model, stmt, spec: QuerySpec, namespace_path: str
    ):
        context_id, ancestor_ids = self._namespace_visibility(namespace_path)
        statuses = [LifecycleStatus.ACTIVE]
        if spec.include_mutable:
            statuses.append(LifecycleStatus.MUTABLE)
        if spec.include_archived:
            statuses.append(LifecycleStatus.ARCHIVED)

        if model is ProcessRun:
            return stmt.where(
                model.namespace_id == context_id,
                model.status.in_(statuses),
            )
        if model in {ProcessTemplate, ResourceTemplate, Resource}:
            return stmt.where(
                model.namespace_id.in_(ancestor_ids),
                model.status.in_(statuses),
            )
        if model is Namespace:
            return stmt.where(model.id.in_(ancestor_ids), model.status.in_(statuses))
        return stmt

    def _apply_namespace_metadata(self, stmt, spec: QuerySpec):
        for key, value in spec.local_metadata_filters.items():
            stmt = stmt.where(Namespace.metadata_json[key].as_string() == value)
        if spec.effective_metadata_filters:
            with self._session_scope() as session:
                repository = NamespaceRepository(session)
                candidate_ids = list(
                    session.scalars(stmt.with_only_columns(Namespace.id))
                )
                matching_ids = [
                    namespace_id
                    for namespace_id in candidate_ids
                    if all(
                        repository.effective_metadata(namespace_id).get(key) == value
                        for key, value in spec.effective_metadata_filters.items()
                    )
                ]
            stmt = stmt.where(Namespace.id.in_(matching_ids))
        return stmt

    def _build_select(  # noqa: C901
        self,
        schema: type[SchemaT],
        spec: QuerySpec,
        namespace_path: str,
        authorization: AuthorizedQuery | None = None,
    ) -> Select:
        try:
            model = SCHEMA_REGISTRY.by_model(schema).orm_model
        except KeyError:
            model = SCHEMA_MODEL_MAPPING[schema]
        stmt = select(model)
        if authorization is None:
            stmt = self._apply_namespace_visibility(model, stmt, spec, namespace_path)
        else:
            context_id, ancestor_ids = self._namespace_visibility(namespace_path)
            stmt = authorization.apply(
                model,
                stmt,
                context_id=context_id,
                ancestor_ids=ancestor_ids,
                include_archived=spec.include_archived,
            )

        if model is ResourceTemplate and "types__names_in" in spec.filters:
            type_names = spec.filters["types__names_in"]
            stmt = (
                stmt.join(ResourceTemplate.types)
                .where(ResourceType.name.in_(type_names))
                .group_by(ResourceTemplate.id)
            )

        filters = {
            key: value
            for key, value in spec.filters.items()
            if key != "types__names_in"
        }
        label = filters.pop("labels__contains", None)
        if label is not None:
            label_values = func.json_each(model.labels).table_valued("value")
            stmt = stmt.where(
                exists(
                    select(1)
                    .select_from(label_values)
                    .where(label_values.c.value == make_slug(label))
                )
            )
        joined_paths: dict[tuple[str, ...], type] = {}
        simple_filters: dict[str, object] = {}

        for raw_key, value in filters.items():
            if "__" not in raw_key:
                simple_filters[raw_key] = value
                continue

        if simple_filters:
            stmt = stmt.filter_by(**simple_filters)

        for raw_key, value in filters.items():
            if "__" not in raw_key:
                continue
            parts = tuple(raw_key.split("__"))
            stmt, attr = resolve_path(model, stmt, parts, joined_paths)
            stmt = stmt.where(attr == value)

        if model is Resource:
            stmt = self._build_select_resource(model, stmt, spec)

        if model is ProcessRun:
            stmt = self._build_select_process_run(model, stmt, spec)

        if model is Namespace:
            stmt = self._apply_namespace_metadata(stmt, spec)
        return _apply_field_expressions(model, stmt, spec, joined_paths)

    def query(
        self, schema: type[SchemaT], spec: QuerySpec, *, namespace_path: str
    ) -> list[SchemaT]:
        return self._materialize_batch(
            schema, self.query_batch(schema, spec, namespace_path=namespace_path)
        )

    def query_authorized(
        self,
        schema: type[SchemaT],
        spec: QuerySpec,
        *,
        authorization: AuthorizedQuery,
    ) -> list[SchemaT]:
        return self._materialize_batch(
            schema,
            self.query_batch_authorized(schema, spec, authorization=authorization),
        )

    def query_batch(
        self, schema: type[SchemaT], spec: QuerySpec, *, namespace_path: str
    ) -> EntityBatch:
        """Collect normalized records directly while ORM rows remain attached."""
        return self._query(schema, spec, namespace_path, authorization=None)

    def query_batch_authorized(
        self, schema: type[SchemaT], spec: QuerySpec, *, authorization: AuthorizedQuery
    ) -> EntityBatch:
        return self._query(
            schema,
            spec,
            authorization.namespace_path,
            authorization=authorization,
        )

    def _query(
        self,
        schema: type[SchemaT],
        spec: QuerySpec,
        namespace_path: str,
        *,
        authorization: AuthorizedQuery | None,
    ) -> EntityBatch:
        stmt = self._build_select(
            schema, spec, namespace_path, authorization=authorization
        )

        # Validate capabilities before opening a session. Selection/filtering and
        # pagination remain separate from requested/supporting graph preparation.
        resource_tree_path = schema is ResourceSchema and (
            spec.load_mode == "eager" or "children" in spec.preloads
        )
        if not resource_tree_path:
            self._relationship_loaders(schema, list(spec.preloads), spec)
        stmt = self._paginate(stmt, spec)

        with self._session_scope() as session:
            return self._query_batch_rows(session, stmt, schema, spec)

    @staticmethod
    def _materialize_batch(schema: type[SchemaT], batch: EntityBatch) -> list[SchemaT]:
        # Import lazily: client package initialization imports LocalBackend.
        from recap.client.identity import IdentityMap
        from recap.client.materialization import BatchMaterializer

        return BatchMaterializer(IdentityMap()).materialize(schema, batch)

    @staticmethod
    def _paginate(stmt, spec):
        if spec.limit is not None:
            stmt = stmt.limit(spec.limit)
        if spec.offset is not None:
            stmt = stmt.offset(spec.offset)
        return stmt

    def _query_batch_rows(self, session, stmt, schema, spec) -> EntityBatch:
        rows = list(session.scalars(stmt).unique())
        return self._collect_query_rows(session, rows, schema, spec)

    def _collect_query_rows(self, session, rows, schema, spec) -> EntityBatch:
        requested = set(spec.preloads)
        full = spec.load_mode == "eager"
        # Existing resource hydrator exposes parent data only for eager reads;
        # preserve that public loading rule while changing representation.
        if schema is ResourceSchema and not full:
            requested.discard("parent")
        if full:
            requested.update(SCHEMA_REGISTRY.by_model(schema).loader_capabilities)
        if schema in {ResourceSchema, ResourceTemplateSchema} and full:
            requested.add("parent")
        if schema is ProcessRunSchema:
            if full:
                requested.add("template")
            if requested & {"steps.parameters", "resources"}:
                requested.add("steps")
        batch = prepare_batch_graph(
            session, rows, schema, requested, spec.on_unloaded or "warn"
        )
        if schema is ResourceTemplateSchema and "parent" not in requested:
            self._mark_root_relation_unloaded(batch, "parent")
        return batch

    @staticmethod
    def _mark_root_relation_unloaded(batch: EntityBatch, relation: str) -> None:
        # Back-references may collect ancestor support without advertising an
        # unrequested relationship on query roots.
        roots = set(batch.roots)
        for edge in batch.adjacency:
            if edge.source in roots and edge.relation == relation:
                edge.loaded = False

    def count(
        self, schema: type[SchemaT], spec: QuerySpec, *, namespace_path: str
    ) -> int:
        return self._count(schema, spec, namespace_path, authorization=None)

    def count_authorized(
        self,
        schema: type[SchemaT],
        spec: QuerySpec,
        *,
        authorization: AuthorizedQuery,
    ) -> int:
        return self._count(
            schema,
            spec,
            authorization.namespace_path,
            authorization=authorization,
        )

    def _count(
        self,
        schema: type[SchemaT],
        spec: QuerySpec,
        namespace_path: str,
        *,
        authorization: AuthorizedQuery | None,
    ) -> int:
        stmt = self._build_select(
            schema, spec, namespace_path, authorization=authorization
        )

        with self._session_scope() as session:
            select_stmt = select(count()).select_from(stmt.subquery())
            return session.execute(select_stmt).scalar_one()

    def _relationship_loaders(
        self, schema: type[SchemaT], preloads: list[str], spec: QuerySpec
    ):
        return resolve_loader_options(schema, preloads, spec.load_mode)

    def get_opts_statements(self, schema, name):
        return preload_options(schema, name)
