from uuid import uuid4

import pytest
from pydantic import ValidationError

from recap.db.process import ProcessRun
from recap.db.resource import Resource, ResourceTemplate
from recap.dsl.query import Field, FieldOrdering, FieldPredicate, QuerySpec
from recap.tests.test_query_dsl import make_query, seed_process_run


def test_field_builds_structured_predicates_and_orderings():
    assert (Field("template.name") == "Acquisition") == FieldPredicate(
        field="template.name", op="eq", value="Acquisition"
    )
    assert (Field("version") != 1) == FieldPredicate(field="version", op="ne", value=1)
    assert (Field("version") < 2).op == "lt"
    assert (Field("version") <= 2).op == "lte"
    assert (Field("version") > 0).op == "gt"
    assert (Field("version") >= 0).op == "gte"
    assert Field("create_date").asc() == FieldOrdering(
        field="create_date", direction="asc"
    )
    assert Field("create_date").desc() == FieldOrdering(
        field="create_date", direction="desc"
    )
    assert Field("name").starts_with("Run-") == FieldPredicate(
        field="name", op="starts_with", value="Run-"
    )
    assert Field("name").ends_with("-done").op == "ends_with"
    assert Field("name").contains("batch").op == "contains"
    assert Field("id").in_((uuid4(), uuid4())).value.__class__ is list
    assert Field("id").not_in([uuid4()]).op == "not_in"


@pytest.mark.parametrize("path", ["", ".name", "template.", "template..name"])
def test_field_rejects_invalid_paths(path):
    with pytest.raises(ValueError, match="field path"):
        Field(path)


@pytest.mark.parametrize("method", ["in_", "not_in"])
@pytest.mark.parametrize("values", ["abc", 1, object()])
def test_membership_rejects_strings_and_non_sequences(method, values):
    with pytest.raises(TypeError, match="sequence"):
        getattr(Field("id"), method)(values)


def test_predicate_rejects_python_boolean_composition():
    with pytest.raises(TypeError, match="and.*or"):
        bool(Field("name") == "sample")


def test_query_spec_normalizes_transport_mappings():
    spec = QuerySpec.model_validate(
        {
            "predicates": [{"field": "name", "op": "eq", "value": "Run-1"}],
            "orderings": [{"field": "create_date", "direction": "desc"}],
        }
    )
    assert spec.predicates == [FieldPredicate(field="name", op="eq", value="Run-1")]
    assert spec.orderings == [FieldOrdering(field="create_date", direction="desc")]


@pytest.mark.parametrize(
    ("key", "mapping"),
    [
        ("predicates", {"field": ".name", "op": "eq", "value": "Run-1"}),
        ("orderings", {"field": "name", "direction": "sideways"}),
    ],
)
def test_query_spec_rejects_invalid_transport_mappings(key, mapping):
    with pytest.raises(ValidationError):
        QuerySpec.model_validate({key: [mapping]})


def test_structured_models_reject_invalid_field_paths():
    with pytest.raises(ValidationError, match="field path"):
        FieldPredicate(field="template.", op="eq", value="Acquisition")
    with pytest.raises(ValidationError, match="field path"):
        FieldOrdering(field=".create_date")


def test_public_query_accepts_structured_predicates_and_orderings(db_session):
    query = (
        make_query(db_session)
        .process_runs()
        .where(Field("name") == "Run-1")
        .order_by(Field("create_date"), Field("name").desc())
    )

    assert query._spec.predicates == [
        FieldPredicate(field="name", op="eq", value="Run-1")
    ]
    assert query._spec.orderings == [
        FieldOrdering(field="create_date", direction="asc"),
        FieldOrdering(field="name", direction="desc"),
    ]


def test_public_query_warns_for_legacy_sqlalchemy_expressions(db_session):
    query = make_query(db_session).process_runs()
    with pytest.warns(DeprecationWarning, match="Field"):
        query = query.where(ProcessRun.name == "Run-1")
    with pytest.warns(DeprecationWarning, match="Field"):
        query.order_by(ProcessRun.name)


def test_structured_predicates_filter_and_order_locally(db_session):
    runs = [
        seed_process_run(db_session, name=f"field-{index}")[1] for index in range(3)
    ]
    names = sorted(run.name for run in runs)

    result = (
        make_query(db_session)
        .process_runs()
        .where(Field("name").starts_with("Run-field-"))
        .where(Field("name").in_(names[1:]))
        .order_by(Field("name").desc())
        .all()
    )

    assert [run.name for run in result] == list(reversed(names[1:]))


@pytest.mark.parametrize(
    ("op", "value_index", "expected_indices"),
    [
        ("eq", 1, [1]),
        ("ne", 1, [0, 2]),
        ("gt", 1, [2]),
        ("gte", 1, [1, 2]),
        ("lt", 1, [0]),
        ("lte", 1, [0, 1]),
        ("in", 1, [1, 2]),
        ("not_in", 1, [0]),
        ("contains", 1, [1]),
        ("starts_with", 1, [1]),
        ("ends_with", 1, [1]),
    ],
)
def test_structured_predicate_operators(db_session, op, value_index, expected_indices):
    names = [f"Run-operator-{op}-{index}" for index in range(3)]
    for index in range(3):
        seed_process_run(db_session, name=f"operator-{op}-{index}")
    field = Field("name")
    if op == "in":
        predicate = field.in_(names[value_index:])
    elif op == "not_in":
        predicate = field.not_in(names[value_index:])
    elif op == "contains":
        predicate = field.contains(names[value_index])
    elif op == "starts_with":
        predicate = field.starts_with(names[value_index])
    elif op == "ends_with":
        predicate = field.ends_with(names[value_index])
    else:
        comparisons = {
            "eq": Field.__eq__,
            "ne": Field.__ne__,
            "gt": Field.__gt__,
            "gte": Field.__ge__,
            "lt": Field.__lt__,
            "lte": Field.__le__,
        }
        predicate = comparisons[op](field, names[value_index])

    result = (
        make_query(db_session)
        .process_runs()
        .where(Field("name").starts_with(f"Run-operator-{op}-"))
        .where(predicate)
        .order_by(Field("name"))
        .all()
    )

    assert [run.name for run in result] == [names[index] for index in expected_indices]


@pytest.mark.parametrize("op", ["contains", "starts_with", "ends_with"])
def test_string_predicates_treat_wildcards_as_literals(db_session, op):
    literal = f"Run-literal-{op}-%_target"
    wildcard_match = f"Run-literal-{op}-XXtarget"
    seed_process_run(db_session, name=f"literal-{op}-%_target")
    seed_process_run(db_session, name=f"literal-{op}-XXtarget")
    values = {
        "contains": "%_target",
        "starts_with": literal,
        "ends_with": "%_target",
    }

    result = (
        make_query(db_session)
        .process_runs()
        .where(getattr(Field("name"), op)(values[op]))
        .all()
    )

    assert [run.name for run in result] == [literal]
    assert wildcard_match not in [run.name for run in result]


def test_structured_predicate_coerces_transport_uuid(db_session):
    _, run = seed_process_run(db_session, name="transport-uuid")
    spec = QuerySpec.model_validate(
        {"predicates": [{"field": "id", "op": "eq", "value": str(run.id)}]}
    )

    result = make_query(db_session).backend.query(
        make_query(db_session).process_runs().model, spec
    )

    assert [item.id for item in result] == [run.id]


def test_structured_predicate_resolves_relationship_and_reuses_join(db_session):
    campaign, run = seed_process_run(db_session, name="shared-join")

    result = (
        make_query(db_session)
        .process_runs()
        .where(Field("campaign.proposal") == campaign.proposal)
        .where(Field("campaign.name") == campaign.name)
        .order_by(Field("campaign.proposal"), Field("name"))
        .all()
    )

    assert [item.id for item in result] == [run.id]


def test_structured_predicate_resolves_self_referential_path(db_session):
    parent = ResourceTemplate(name="field-parent", version="1.0")
    child = ResourceTemplate(name="field-child", version="1.0", parent=parent)
    resource = Resource(name="field-resource", template=child)
    db_session.add_all([parent, child, resource])
    db_session.commit()

    result = (
        make_query(db_session)
        .resources()
        .where(Field("template.parent.name") == parent.name)
        .where(Field("template.parent.version") == parent.version)
        .order_by(Field("template.parent.name"))
        .all()
    )

    assert [item.id for item in result] == [resource.id]


def test_structured_and_legacy_path_filters_share_joins(db_session):
    _, run = seed_process_run(db_session, name="legacy-shared")

    result = (
        make_query(db_session)
        .process_runs()
        .filter(template__name=run.template.name)
        .where(Field("template.version") == run.template.version)
        .all()
    )

    assert [item.id for item in result] == [run.id]


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("missing", "has no attribute 'missing'"),
        ("missing.name", "has no relationship 'missing'"),
        ("campaign", "is a relationship, not a column"),
    ],
)
def test_structured_predicate_rejects_invalid_paths(db_session, field, message):
    with pytest.raises(ValueError, match=message):
        make_query(db_session).process_runs().where(Field(field) == "value").all()


def test_structured_predicate_reports_field_coercion_failure(db_session):
    with pytest.raises(ValueError, match="id.*not-a-uuid"):
        make_query(db_session).process_runs().where(Field("id") == "not-a-uuid").all()


def test_string_predicate_rejects_non_string_column(db_session):
    with pytest.raises(ValueError, match="id.*string"):
        make_query(db_session).process_runs().where(Field("id").contains("value")).all()
