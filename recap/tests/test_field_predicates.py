from uuid import uuid4

import pytest
from pydantic import ValidationError

from recap.db.process import ProcessRun
from recap.dsl.query import Field, FieldOrdering, FieldPredicate, QuerySpec
from recap.tests.test_query_dsl import make_query


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


@pytest.mark.parametrize("values", ["abc", 1, object()])
def test_membership_rejects_strings_and_non_sequences(values):
    with pytest.raises(TypeError, match="sequence"):
        Field("id").in_(values)


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
