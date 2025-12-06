from datetime import UTC, datetime, timedelta

import pytest

from recap.db.exceptions import ValidationError
from recap.db.process import Direction


def test_platemate_via_client_child_steps(client):  # noqa
    """
    Replicate the PlateMate workflow using RecapClient, modeling harvesting as
    child steps with step-level resource assignments (well -> pin) and step
    parameters (arrival/departure/lsdc_name/harvested).
    """
    base_time = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    lib_wells = [f"A0{i}" for i in range(1, 4)]

    # Resource templates
    with client.build_resource_template(
        "PM Library Plate", ["container", "library_plate", "plate"]
    ) as lp:
        lp.add_properties(
            {
                "dimensions": [
                    {"name": "rows", "type": "int", "default": 3},
                    {"name": "columns", "type": "int", "default": 1},
                ]
            }
        )
        for idx, well in enumerate(lib_wells, start=1):
            (
                lp.add_child(well, ["container", "well"])
                .add_properties(
                    {
                        "status": [
                            {"name": "used", "type": "bool", "default": False},
                        ],
                        "content": [
                            {"name": "catalog_id", "type": "str", "default": ""},
                            {"name": "smiles", "type": "str", "default": ""},
                            {"name": "sequence", "type": "int", "default": idx},
                        ],
                    }
                )
                .close_child()
            )

    with client.build_resource_template(
        "PM Xtal Plate", ["container", "xtal_plate", "plate"]
    ) as xt:
        xt.add_properties(
            {
                "settings": [
                    {
                        "name": "drop_volume",
                        "type": "float",
                        "unit": "nL",
                        "default": 100,
                    }
                ]
            }
        )
        mapping = [
            {"name": "A1a", "echo": "A1", "drop": "c", "origin_y": 0},
            {"name": "A1b", "echo": "A2", "drop": "lu", "origin_y": 1350},
            {"name": "A2a", "echo": "A3", "drop": "dr", "origin_y": 0},
        ]
        drop_offsets = {"c": (0, 0), "lu": (-300, 300), "dr": (300, -300)}
        for m in mapping:
            x_off, y_off = drop_offsets[m["drop"]]
            (
                xt.add_child(m["name"], ["container", "well"])
                .add_properties(
                    {
                        "mapping": [
                            {
                                "name": "echo_position",
                                "type": "str",
                                "default": m["echo"],
                            },
                            {"name": "well_origin_x", "type": "int", "default": 0},
                            {
                                "name": "well_origin_y",
                                "type": "int",
                                "default": m["origin_y"],
                            },
                        ],
                        "drop": [
                            {"name": "code", "type": "str", "default": m["drop"]},
                            {"name": "x_offset", "type": "int", "default": x_off},
                            {"name": "y_offset", "type": "int", "default": y_off},
                        ],
                    }
                )
                .close_child()
            )

    with client.build_resource_template(
        "Puck Collection", ["container", "puck_collection"]
    ) as pc:
        pc.add_child("FGZ001", ["container", "puck"])
        pc.add_child("FGZ002", ["container", "puck"])

    with client.build_resource_template("PM Puck", ["container", "puck"]) as pk:
        for idx in range(1, 4):
            (
                pk.add_child(f"Pin-{idx}", ["container", "pin"])
                .add_properties(
                    {
                        "mount": [
                            {"name": "position", "type": "int", "default": idx},
                            {"name": "sample_name", "type": "str", "default": ""},
                            {"name": "departure", "type": "datetime", "default": None},
                        ]
                    }
                )
                .close_child()
            )

    # Process template
    with client.build_process_template("PM Workflow", "1.0") as pt:
        (
            pt.add_resource_slot("library_plate", "library_plate", Direction.input)
            .add_resource_slot("xtal_plate", "xtal_plate", Direction.input)
            .add_resource_slot("puck_collection", "puck_collection", Direction.output)
        )
        (
            pt.add_step(name="Echo Transfer")
            .param_group("echo")
            .add_attribute("batch", "int", "", 1)
            .add_attribute("volume", "float", "nL", 25.0)
            .close_group()
            .bind_slot("source_plate", "library_plate")
            .bind_slot("dest_plate", "xtal_plate")
            .close_step()
        )
        (
            pt.add_step(name="Harvesting")
            .param_group("harvest")
            .add_attribute("arrival", "datetime", "", None)
            .add_attribute("departure", "datetime", "", None)
            .add_attribute("lsdc_name", "str", "", "")
            .add_attribute("harvested", "bool", "", False)
            .close_group()
            .bind_slot("source_plate", "xtal_plate")
            .bind_slot("dest_puck", "puck_collection")
            .close_step()
        )

    client.create_campaign("PM Campaign", "PM-001")

    # Resources
    with client.build_resource("DSI-poised", "PM Library Plate") as lib_plate:
        for well in lib_plate.resource.children:
            well.properties["content"].values["catalog_id"] = f"CAT-{well.name}"
            well.properties["content"].values["smiles"] = f"SMILES-{well.name}"

    with client.build_resource("pmtest", "PM Xtal Plate"):
        pass

    # Build puck collection and ensure duplicate child creation raises
    collection_ref = client.create_resource("Test Puck Collection", "Puck Collection")
    uow = client.backend.begin()
    try:
        puck_template = client.backend.get_resource_template("PM Puck")
        for puck_name in ["FGZ001", "FGZ002"]:
            with pytest.raises(ValidationError):
                client.backend.create_resource(
                    puck_name,
                    puck_template,
                    parent_resource=collection_ref,
                )
        # Add a uniquely named puck to verify creation still works
        client.backend.create_resource(
            "FGZ003",
            puck_template,
            parent_resource=collection_ref,
        )
        uow.commit()
    except Exception:
        uow.rollback()
        raise

    def populate_children(resource_name: str, template_name: str):
        """Ensure a resource has instantiated children from its template."""
        uow_inner = client.backend.begin()
        try:
            res = client.backend.get_resource(resource_name, template_name)
            tmpl = client.backend.get_resource_template(template_name, expand=True)
            # If children already exist, skip creation
            existing_children = client.backend.get_resource(
                resource_name, template_name, expand=True
            ).children
            if existing_children:
                uow_inner.commit()
                return
            for child_tmpl in tmpl.children:
                client.backend.create_resource(
                    child_tmpl.name, child_tmpl, parent_resource=res
                )
            uow_inner.commit()
        except Exception:
            uow_inner.rollback()
            raise

    # Ensure wells/pins are materialized
    def seed_children(parent_name: str, parent_template: str, child_names: list[str]):
        uow_seed = client.backend.begin()
        try:
            parent_ref = client.backend.get_resource(parent_name, parent_template)
            for child_name in child_names:
                try:
                    client.backend.get_resource(child_name, child_name)
                    continue
                except Exception:
                    child_template = client.backend.get_resource_template(child_name)
                    client.backend.create_resource(
                        child_name, child_template, parent_resource=parent_ref
                    )
            uow_seed.commit()
        except Exception:
            uow_seed.rollback()
            raise

    seed_children("DSI-poised", "PM Library Plate", lib_wells)
    xtal_child_names = [m["name"] for m in mapping]
    seed_children("pmtest", "PM Xtal Plate", xtal_child_names)

    # Process run + assignments
    with client.build_process_run(
        "pm-run", "Platemate via client", "PM Workflow", "1.0"
    ) as prb:
        prb.assign_resource("library_plate", "DSI-poised", "PM Library Plate")
        prb.assign_resource("xtal_plate", "pmtest", "PM Xtal Plate")
        prb.assign_resource(
            "puck_collection", "Test Puck Collection", "Puck Collection"
        )

        echo_params = prb.get_params("Echo Transfer")
        echo_params.echo.batch = 7
        echo_params.echo.volume = 25.0
        prb.set_params(echo_params)

        # Child steps for echo transfer with step-level assignments (lib well -> xtal well)
        xtal_wells = sorted(
            client.backend.get_resource(
                "pmtest", "PM Xtal Plate", expand=True
            ).children,
            key=lambda w: w.name,
        )
        lib_children = sorted(
            client.backend.get_resource(
                "DSI-poised", "PM Library Plate", expand=True
            ).children,
            key=lambda w: w.name,
        )
        assert len(xtal_wells) == 3, "xtal wells not initialized"
        assert len(lib_children) == 3, "library wells not initialized"
        echo_parent = next(step for step in prb.steps if step.name == "Echo Transfer")
        echo_children_created = []
        for source, dest in zip(lib_children, xtal_wells, strict=False):
            echo_child = prb.add_child_step(
                parent_step_name=echo_parent.name,
                step_template_name="Echo Transfer",
                parameters={
                    "echo": {
                        "batch": 7,
                        "volume": echo_params.echo.volume,
                    }
                },
                resources={"source_plate": source, "dest_plate": dest},
            )
            echo_children_created.append(echo_child)
        assert len(echo_children_created) == 3

        # Child steps for harvesting with step-level assignments (well -> pin inside a puck collection)
        puck_collection = client.backend.get_resource(
            "Test Puck Collection", "Puck Collection", expand=True
        )
        # Use the explicitly created puck with pins (FGZ003) for harvesting
        puck_with_pins = next(
            (p for p in puck_collection.children if p.name == "FGZ003"), None
        )
        assert puck_with_pins is not None, "FGZ003 puck not initialized"
        puck_pins = sorted(puck_with_pins.children, key=lambda p: p.name)
        assert len(puck_pins) >= 2, "puck pins not initialized"
        harvest_parent = next(step for step in prb.steps if step.name == "Harvesting")
        harvest_children_created = []

        for idx, dest in enumerate(xtal_wells[:2]):
            arrival = base_time + timedelta(minutes=5 + idx * 10)
            departure = arrival + timedelta(minutes=5)
            harvest_params = {
                "harvest": {
                    "arrival": arrival,
                    "departure": departure,
                    "lsdc_name": f"mpro-{idx + 1:02d}",
                    "harvested": True,
                }
            }
            harvest_child = prb.add_child_step(
                parent_step_name=harvest_parent.name,
                step_template_name="Harvesting",
                parameters=harvest_params,
                resources={
                    "source_plate": dest,
                    "dest_puck": puck_pins[idx],
                },
            )
            lib_children[idx].properties["status"].values["used"] = True
            harvest_children_created.append(harvest_child)
        assert len(harvest_children_created) == 2

    # Echo rows derived from echo child steps + mapping properties
    # Echo rows from creation-time pairs (source/dest) plus dest mapping
    echo_rows = []
    for source, dest in zip(lib_children, xtal_wells, strict=False):
        mapping = dest.properties["mapping"].values
        drop = dest.properties["drop"].values
        echo_rows.append(
            {
                "dest": mapping["echo_position"],
                "source": source.name,
                "volume": echo_params.echo.volume,
                "x": mapping["well_origin_x"] + drop["x_offset"],
                "y": mapping["well_origin_y"] + drop["y_offset"],
            }
        )

    assert [r["dest"] for r in echo_rows] == ["A1", "A2", "A3"]
    assert echo_rows[1]["y"] == 1650
    assert all(r["volume"] == 25.0 for r in echo_rows)

    # Harvest manifest via child steps
    manifest = []
    summary = []
    for idx, dest in enumerate(xtal_wells[:2]):
        arrival = base_time + timedelta(minutes=5 + idx * 10)
        departure = arrival + timedelta(minutes=5)
        catalog = f"CAT-{lib_children[idx].name}"
        lib_children[idx].properties["content"].values["catalog_id"] = catalog
        manifest.append(
            {
                "sample": f"mpro-{idx + 1:02d}",
                "arrival": arrival,
                "departure": departure,
                "source": dest.name,
                "dest": puck_pins[idx].name,
            }
        )
        summary.append(
            {
                "sample": f"mpro-{idx + 1:02d}",
                "catalog": catalog,
                "smiles": lib_children[idx].properties["content"].values["smiles"],
                "soak_min": round((departure - base_time).total_seconds() / 60, 1),
                "harvest_sec": round((departure - arrival).total_seconds(), 1),
                "dest_resource": puck_pins[idx].name,
            }
        )

    assert [m["sample"] for m in manifest] == ["mpro-01", "mpro-02"]
    assert all(isinstance(m["arrival"], datetime) for m in manifest)
    assert summary[0]["harvest_sec"] == 300.0
    assert summary[1]["catalog"] == "CAT-A02"
    assert summary[1]["dest_resource"] == "Pin-2"

    # API gaps to consider:
    # - bulk child creation in resource builders
    # - helper to map source/dest children (library wells -> xtal wells)
    # - helper to export echo CSV/harvest manifests from a process_run
