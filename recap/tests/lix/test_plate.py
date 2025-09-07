from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from recap.schemas.experiment import WorkflowData


def test_plate_yaml():
    file_path = Path(__file__).parent.parent / Path("./test_data/lix_containers.yml")
    with file_path.open("r") as f:
        data = yaml.safe_load(f)

    try:
        container_data = WorkflowData.model_validate(data)
        assert container_data.container_types, "No container_types found in YAML"
    except ValidationError as e:
        pytest.fail(f"Failed to validate YAML: {e}")
