import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from recap.server.audit import AuditOutcome, AuditRecord, AuditSink


@pytest.mark.parametrize(
    ("outcome", "reason_code"),
    [
        (AuditOutcome.SUCCESS, None),
        (AuditOutcome.DENIED, "insufficient_scope"),
        (AuditOutcome.ERROR, "service_unavailable"),
    ],
)
def test_audit_records_serialize_only_sanitized_mutation_context(outcome, reason_code):
    record = AuditRecord(
        request_id=uuid4(),
        actor_id="actor-1",
        mutation="create_resource",
        resource_type="resource",
        resource_id="resource-1",
        outcome=outcome,
        reason_code=reason_code,
    )

    serialized = json.dumps(record.model_dump(mode="json"), sort_keys=True)
    assert set(record.model_dump()) == {
        "request_id",
        "actor_id",
        "mutation",
        "resource_type",
        "resource_id",
        "outcome",
        "reason_code",
    }
    assert "credential" not in serialized
    assert "header" not in serialized
    assert "property_value" not in serialized
    assert "parameter_value" not in serialized
    assert "grant" not in serialized


@pytest.mark.parametrize(
    "forbidden_field",
    [
        "credential",
        "headers",
        "property_values",
        "parameter_values",
        "grant_details",
    ],
)
def test_audit_record_rejects_forbidden_detail_fields(forbidden_field):
    data = {
        "request_id": uuid4(),
        "actor_id": "actor-1",
        "mutation": "create_resource",
        "resource_type": "resource",
        "resource_id": "resource-1",
        "outcome": "success",
        forbidden_field: "sensitive-value",
    }

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AuditRecord.model_validate(data)


def test_audit_outcome_and_reason_are_consistent():
    common = {
        "request_id": uuid4(),
        "actor_id": "actor-1",
        "mutation": "create_resource",
        "resource_type": "resource",
        "resource_id": "resource-1",
    }

    with pytest.raises(ValidationError):
        AuditRecord(**common, outcome="success", reason_code="internal-detail")
    with pytest.raises(ValidationError):
        AuditRecord(**common, outcome="denied")
    with pytest.raises(ValidationError):
        AuditRecord(**common, outcome="error")

    with pytest.raises(ValidationError):
        AuditRecord(
            **common,
            outcome="error",
            reason_code="credential=secret grant=admin",
        )


def test_audit_sink_is_structural_protocol():
    class Collector:
        def emit(self, record: AuditRecord) -> None:
            pass

    assert isinstance(Collector(), AuditSink)
