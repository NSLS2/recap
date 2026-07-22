def test_strawberry_types_importable():
    from recap.server.strawberry_types import (
        CampaignType,
        ProcessRunType,
        ProcessTemplateType,
        ResourceType,
        ResourceTemplateType,
        StepType,
    )
    assert CampaignType is not None


def test_build_schema_importable():
    from recap.server.strawberry_schema import build_schema
    assert callable(build_schema)
