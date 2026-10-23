"""Requested ORM relationship projections, independent of model hydration."""

from collections.abc import Iterable
from typing import Any
from uuid import UUID


def index_children(rows: Iterable[Any]) -> dict[UUID, list[Any]]:
    """Index flat rows by parent foreign key without accessing relationships."""
    children: dict[UUID, list[Any]] = {}
    for row in rows:
        if row.parent_id is not None:
            children.setdefault(row.parent_id, []).append(row)
    return children


def resource_relations(
    source: Any,
    requested: set[str],
    children_map: dict[UUID, list[Any]] | None = None,
) -> dict[str, Any]:
    relations = {}
    for name in ("template", "parent", "properties"):
        if name in requested:
            relations[name] = getattr(source, name)
    if "children" in requested:
        relations["children"] = (
            {child.name: child for child in children_map.get(source.id, [])}
            if children_map is not None
            else source.children
        )
    return relations


def resource_template_relations(source: Any, requested: set[str]) -> dict[str, Any]:
    return {
        name: getattr(source, name)
        for name in ("parent", "types", "children", "attribute_group_templates")
        if name in requested
    }


def step_template_relations(source: Any, requested: set[str]) -> dict[str, Any]:
    relations = {}
    if "attribute_group_templates" in requested:
        relations["attribute_group_templates"] = source.attribute_group_templates
    if "resource_slots" in requested:
        relations["resource_slots"] = {
            binding.role: binding.resource_slot for binding in source.bindings.values()
        }
    return relations


def process_template_relations(source: Any, requested: set[str]) -> dict[str, Any]:
    return {
        name: getattr(source, name)
        for name in ("step_templates", "resource_slots")
        if name in requested
    }


def process_run_relations(source: Any, requested: set[str]) -> dict[str, Any]:
    return {
        name: getattr(source, name)
        for name in ("template", "steps", "assigned_resources")
        if name in requested
    }
