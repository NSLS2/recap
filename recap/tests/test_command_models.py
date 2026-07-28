from uuid import uuid4

import pytest
from pydantic import ValidationError

from recap.commands.models import CreateResource


def test_create_resource_forbids_server_fields():
    with pytest.raises(ValidationError, match="namespace_id"):
        CreateResource(
            namespace_path="beamline/endstation",
            name="robot",
            template_id=uuid4(),
            namespace_id=uuid4(),
        )


@pytest.mark.parametrize("field", ["id", "status", "revision"])
def test_create_resource_forbids_other_server_fields(field):
    values = {
        "namespace_path": "beamline/endstation",
        "name": "robot",
        "template_id": uuid4(),
        field: "server-controlled",
    }

    with pytest.raises(ValidationError, match=field):
        CreateResource(**values)


def test_create_resource_is_immutable():
    command = CreateResource(
        namespace_path="beamline/endstation",
        name="robot",
        template_id=uuid4(),
    )

    with pytest.raises(ValidationError, match="frozen"):
        command.name = "replacement"
