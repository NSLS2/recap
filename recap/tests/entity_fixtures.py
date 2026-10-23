"""Attached ORM graphs shared by collection, materialization, and transport tests."""

from uuid import uuid4

from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.db.namespace import Namespace
from recap.db.process import ProcessTemplate
from recap.db.resource import Resource, ResourceTemplate, ResourceType
from recap.db.step import StepTemplate


def _seed_resource_graph(db_session):
    namespace = Namespace(path=f"entities/{uuid4().hex}", metadata_json={})
    resource_type = ResourceType(name=f"entity-type-{uuid4().hex}")
    parent_template = ResourceTemplate(
        name=f"ParentTemplate-{uuid4().hex}", namespace=namespace
    )
    parent_template.types.append(resource_type)
    property_group = AttributeGroupTemplate(name="Measurements")
    property_group.attribute_templates.append(
        AttributeTemplate(name="dose", value_type="int", default_value="1")
    )
    parent_template.attribute_group_templates.append(property_group)
    child_template = ResourceTemplate(
        name="child", namespace=namespace, parent=parent_template
    )
    parent = Resource(name="parent", template=parent_template, namespace=namespace)
    child = parent.children["child"]
    db_session.add_all(
        [namespace, resource_type, parent_template, child_template, parent]
    )
    db_session.flush()
    return parent, child, parent_template, child_template


def _seed_process_template(db_session):
    namespace = Namespace(path=f"entities/process-{uuid4().hex}", metadata_json={})
    process_template = ProcessTemplate(
        name=f"Process-{uuid4().hex}", version="1.0", namespace=namespace
    )
    step_template = StepTemplate(name="Collect", process_template=process_template)
    parameter_group = AttributeGroupTemplate(name="Exposure")
    parameter_group.attribute_templates.append(
        AttributeTemplate(name="dwell", value_type="int", default_value="1")
    )
    step_template.attribute_group_templates.append(parameter_group)
    db_session.add_all([namespace, process_template, step_template])
    db_session.flush()
    return process_template, step_template
