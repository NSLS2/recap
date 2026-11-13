"""Performance cases split without changing assertions or marks."""

import pytest


@pytest.mark.performance
@pytest.mark.parametrize(
    "client_fixture", ["client", "rest_loopback_client"], ids=["local", "rest"]
)
def test_client_batch_reads_reuse_canonical_identity(request, client_fixture):
    client = request.getfixturevalue(client_fixture)
    with client.build_resource_template(name="BatchPath", type_names=["sample"]):
        pass
    resource = client.create_resource("sample", "BatchPath")
    with client.build_resource(resource_id=resource.id) as builder:
        builder.finalize()
    first = client.query_maker().resources(load="eager").filter(id=resource.id).first()
    second = client.query_maker().resources(load="eager").filter(id=resource.id).first()
    assert first is second
