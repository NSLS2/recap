"""Select shared loaded-plate graph roots for SQL profiling."""

from recap.tests.helpers.sql_profile import profile_read


def read_graph(factory, graph, root, kind, preloads, snapshot):
    object_id = graph.well_ids[0] if root == "well" else getattr(graph, f"{root}_id")
    return profile_read(
        factory,
        graph.namespace_path,
        object_id,
        kind=kind,
        preloads=preloads,
        snapshot=snapshot,
    )
