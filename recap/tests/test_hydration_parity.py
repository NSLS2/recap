from uuid import uuid4

from recap.adapter.entity_hydration import EntityHydrationContext
from recap.adapter.process_run_construct import ProcessRunSchemaHydrator
from recap.adapter.resource_construct import ResourceSchemaHydrator
from recap.adapter.transport import QueryResult, hydrate_result, serialize_model
from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.db.namespace import Namespace
from recap.db.process import ProcessRun, ProcessTemplate
from recap.db.resource import Resource, ResourceTemplate, ResourceType
from recap.db.step import StepTemplate


def _seed_metadata_graph(db_session):
    namespace = Namespace(path=f"hydration/{uuid4().hex}", metadata_json={})
    resource_type = ResourceType(name=f"sample-{uuid4().hex}")
    resource_template = ResourceTemplate(
        name=f"Resource-{uuid4().hex}", namespace=namespace
    )
    resource_template.types.append(resource_type)
    resource_group = AttributeGroupTemplate(name="Measurements")
    resource_group.attribute_templates.append(
        AttributeTemplate(name="dose", value_type="int", default_value="1")
    )
    resource_template.attribute_group_templates.append(resource_group)
    resource = Resource(name="sample", template=resource_template, namespace=namespace)
    resource.properties["Measurements"]._values["dose"].metadata_json = {
        "source": "header"
    }

    process_template = ProcessTemplate(
        name=f"Process-{uuid4().hex}", version="1.0", namespace=namespace
    )
    step_template = StepTemplate(name="Collect", process_template=process_template)
    parameter_group = AttributeGroupTemplate(name="Exposure")
    parameter_group.attribute_templates.append(
        AttributeTemplate(name="dwell", value_type="int", default_value="1")
    )
    step_template.attribute_group_templates.append(parameter_group)
    run = ProcessRun(
        name=f"run-{uuid4().hex}",
        description="hydration parity",
        template=process_template,
        namespace=namespace,
    )
    run.steps["Collect"].parameters["Exposure"]._values["dwell"].metadata_json = {
        "source": "header"
    }
    db_session.add_all(
        [namespace, resource_type, resource_template, process_template, step_template, resource, run]
    )
    db_session.flush()
    return resource, run


def test_property_and_parameter_metadata_survive_direct_and_embedded_hydration(
    db_session,
):
    resource, run = _seed_metadata_graph(db_session)

    direct = ResourceSchemaHydrator().construct_many(
        [resource],
        include_template=True,
        include_properties=True,
        include_children=False,
        full=False,
        on_unloaded="warn",
    )[0]
    embedded = ProcessRunSchemaHydrator().construct_many(
        [run],
        include_steps=True,
        include_step_parameters=True,
        include_resources=False,
        include_template=False,
        full=False,
        on_unloaded="warn",
    )[0]

    assert direct.properties["Measurements"].values.dose.metadata_json == {
        "source": "header"
    }
    assert direct.is_loaded("template")
    assert direct.is_loaded("properties")
    assert not direct.is_loaded("children")
    assert embedded.is_loaded("steps")
    assert not embedded.is_loaded("assigned_resources")
    assert embedded.steps["Collect"].parameters["Exposure"].values.dwell.metadata_json == {
        "source": "header"
    }

    result = QueryResult(
        entity="resource", projection="full", items=[serialize_model(direct)]
    )
    round_tripped = hydrate_result(type(direct), result)[0]
    assert round_tripped.properties["Measurements"].values.dose.metadata_json == {
        "source": "header"
    }


def test_shared_context_reuses_resource_across_direct_and_embedded_paths(db_session):
    resource, _ = _seed_metadata_graph(db_session)
    context = EntityHydrationContext()
    direct = ResourceSchemaHydrator(context).construct_many(
        [resource],
        include_template=True,
        include_properties=True,
        include_children=False,
        full=False,
        on_unloaded="warn",
    )[0]
    process_hydrator = ProcessRunSchemaHydrator(context)

    embedded_resource = process_hydrator._construct_resource_schema(resource)

    assert embedded_resource is direct
    assert embedded_resource.is_loaded("children")
    assert embedded_resource.is_loaded("properties")
