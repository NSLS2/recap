from datetime import UTC, datetime, timedelta
from uuid import uuid4

from recap.db.attribute import AttributeGroupTemplate, AttributeTemplate
from recap.db.campaign import Campaign
from recap.db.process import Direction, ProcessRun, ProcessTemplate, ResourceSlot
from recap.db.resource import Resource, ResourceTemplate, ResourceType
from recap.db.step import StepTemplate
from recap.utils.database import get_or_create


def _make_attr_group(name: str, attrs: list[dict]) -> AttributeGroupTemplate:
    group = AttributeGroupTemplate(name=name)
    for attr in attrs:
        group.attribute_templates.append(
            AttributeTemplate(
                name=attr["name"],
                value_type=attr["value_type"],
                unit=attr.get("unit"),
                default_value=attr.get("default"),
            )
        )
    return group


def test_platemate_workflow_with_recap(db_session):  # noqa
    """
    Replicate the PlateMate flow (library plate → xtal plate → puck) using
    Recap's resource/process model. The goal is to prove Recap can capture the
    same provenance as PlateMate's bespoke schema.
    """
    suffix = uuid4().hex[:8]
    base_time = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    drop_offsets = {
        "c": (0, 0),
        "lu": (-300, 300),
        "dr": (300, -300),
    }

    container, _ = get_or_create(db_session, ResourceType, where={"name": "container"})
    plate_type, _ = get_or_create(db_session, ResourceType, where={"name": "plate"})
    puck_type, _ = get_or_create(db_session, ResourceType, where={"name": "puck"})

    lib_plate_template = ResourceTemplate(
        name="Platemate Library Plate",
        types=[container, plate_type],
    )
    lib_plate_template.attribute_group_templates.append(
        _make_attr_group(
            "dimensions",
            [
                {"name": "rows", "value_type": "int", "default": 3},
                {"name": "columns", "value_type": "int", "default": 1},
            ],
        )
    )
    for sequence, well_name in enumerate(["A01", "A02", "A03"], start=1):
        well_template = ResourceTemplate(
            name=well_name,
            types=[container],
            attribute_group_templates=[
                _make_attr_group(
                    "well_status",
                    [{"name": "used", "value_type": "bool", "default": False}],
                ),
                _make_attr_group(
                    "content",
                    [
                        {"name": "catalog_id", "value_type": "str", "default": ""},
                        {"name": "smiles", "value_type": "str", "default": ""},
                        {"name": "sequence", "value_type": "int", "default": sequence},
                    ],
                ),
            ],
        )
        lib_plate_template.children.append(well_template)

    xtal_plate_template = ResourceTemplate(
        name="Platemate Xtal Plate",
        types=[container, plate_type],
        attribute_group_templates=[
            _make_attr_group(
                "plate_settings",
                [{"name": "drop_volume_nl", "value_type": "float", "default": 100.0}],
            )
        ],
    )
    xtal_maps = [
        {"name": "A1a", "echo": "A1", "drop": "c", "origin_y": 0},
        {"name": "A1b", "echo": "A2", "drop": "lu", "origin_y": 1350},
        {"name": "A2a", "echo": "A3", "drop": "dr", "origin_y": 0},
    ]
    for xtal_map in xtal_maps:
        x_offset, y_offset = drop_offsets[xtal_map["drop"]]
        xtal_template = ResourceTemplate(
            name=xtal_map["name"],
            types=[container],
            attribute_group_templates=[
                _make_attr_group(
                    "mapping",
                    [
                        {
                            "name": "shifter",
                            "value_type": "str",
                            "default": xtal_map["name"],
                        },
                        {
                            "name": "echo_position",
                            "value_type": "str",
                            "default": xtal_map["echo"],
                        },
                        {"name": "well_origin_x", "value_type": "int", "default": 0},
                        {
                            "name": "well_origin_y",
                            "value_type": "int",
                            "default": xtal_map["origin_y"],
                        },
                    ],
                ),
                _make_attr_group(
                    "drop_location",
                    [
                        {
                            "name": "drop_code",
                            "value_type": "str",
                            "default": xtal_map["drop"],
                        },
                        {"name": "x_offset", "value_type": "int", "default": x_offset},
                        {"name": "y_offset", "value_type": "int", "default": y_offset},
                    ],
                ),
                _make_attr_group(
                    "transfer",
                    [
                        {"name": "source_well", "value_type": "str", "default": ""},
                        {"name": "volume_nl", "value_type": "float", "default": 0.0},
                    ],
                ),
                _make_attr_group(
                    "harvest",
                    [
                        {
                            "name": "arrival_time",
                            "value_type": "datetime",
                            "default": None,
                        },
                        {
                            "name": "departure_time",
                            "value_type": "datetime",
                            "default": None,
                        },
                        {"name": "puck", "value_type": "str", "default": ""},
                        {"name": "pin_position", "value_type": "int", "default": 0},
                        {"name": "lsdc_name", "value_type": "str", "default": ""},
                        {"name": "harvested", "value_type": "bool", "default": False},
                    ],
                ),
            ],
        )
        xtal_plate_template.children.append(xtal_template)

    puck_template = ResourceTemplate(
        name="Platemate Puck",
        types=[container, puck_type],
    )
    for idx in range(1, 4):
        pin_template = ResourceTemplate(
            name=f"Pin-{idx}",
            types=[container],
            attribute_group_templates=[
                _make_attr_group(
                    "mount",
                    [
                        {"name": "position", "value_type": "int", "default": idx},
                        {"name": "sample_name", "value_type": "str", "default": ""},
                        {
                            "name": "departure_time",
                            "value_type": "datetime",
                            "default": None,
                        },
                    ],
                )
            ],
        )
        puck_template.children.append(pin_template)

        process_template = ProcessTemplate(
            name=f"Platemate Workflow {suffix}", version="1.0"
        )
    lib_slot = ResourceSlot(
        name="library_plate",
        process_template=process_template,
        resource_type=plate_type,
        direction=Direction.input,
    )
    xtal_slot = ResourceSlot(
        name="xtal_plate",
        process_template=process_template,
        resource_type=plate_type,
        direction=Direction.input,
    )
    puck_slot = ResourceSlot(
        name="puck",
        process_template=process_template,
        resource_type=puck_type,
        direction=Direction.output,
    )
    echo_step_template = StepTemplate(
        name="Echo Transfer",
        process_template=process_template,
        attribute_group_templates=[
            _make_attr_group(
                "echo_settings",
                [
                    {"name": "batch_id", "value_type": "int", "default": 1},
                    {"name": "volume_nl", "value_type": "float", "default": 25.0},
                ],
            )
        ],
    )
    echo_step_template.resource_slots["source_plate"] = lib_slot
    echo_step_template.resource_slots["dest_plate"] = xtal_slot

    harvest_step_template = StepTemplate(
        name="Harvesting",
        process_template=process_template,
        attribute_group_templates=[
            _make_attr_group(
                "harvest_defaults",
                [{"name": "default_drop", "value_type": "str", "default": "c"}],
            )
        ],
    )
    harvest_step_template.resource_slots["source_plate"] = xtal_slot
    harvest_step_template.resource_slots["dest_puck"] = puck_slot

    process_template.step_templates.extend([echo_step_template, harvest_step_template])
    db_session.add_all(
        [
            lib_plate_template,
            xtal_plate_template,
            puck_template,
            process_template,
        ]
    )
    db_session.flush()

    lib_plate = Resource(name=f"DSI-poised-{suffix}", template=lib_plate_template)
    xtal_plate = Resource(name=f"pmtest-{suffix}", template=xtal_plate_template)
    puck = Resource(name=f"FGZ001-{suffix}", template=puck_template)
    campaign = Campaign(
        name=f"Platemate Campaign {suffix}",
        proposal=f"PM-{suffix}",
        meta_data={"target": "mpro"},
    )
    process_run = ProcessRun(
        name=f"pm-run-{suffix}",
        description="Replicated PlateMate flow with Recap",
        template=process_template,
        campaign=campaign,
    )
    process_run.create_date = base_time

    db_session.add_all([lib_plate, xtal_plate, puck, campaign, process_run])
    process_run.resources[lib_slot] = lib_plate
    process_run.resources[xtal_slot] = xtal_plate
    process_run.resources[puck_slot] = puck

    library_wells = sorted(lib_plate.children, key=lambda w: w.name)
    xtal_wells = sorted(xtal_plate.children, key=lambda w: w.name)
    pins = sorted(puck.children, key=lambda p: p.properties["mount"].values["position"])

    sample_meta = [
        {"catalog_id": "CAT-001", "smiles": "CCO"},
        {"catalog_id": "CAT-002", "smiles": "N#N"},
        {"catalog_id": "CAT-003", "smiles": "CCC"},
    ]
    for well, meta in zip(library_wells, sample_meta, strict=False):
        content = well.properties["content"]
        content.values["catalog_id"] = meta["catalog_id"]
        content.values["smiles"] = meta["smiles"]

    echo_step = next(step for step in process_run.steps if step.name == "Echo Transfer")
    echo_settings = echo_step.parameters["echo_settings"]
    echo_settings.values["batch_id"] = 7
    echo_settings.values["volume_nl"] = 25.0

    for lib_well, xtal_well in zip(library_wells, xtal_wells, strict=False):
        transfer = xtal_well.properties["transfer"]
        transfer.values["source_well"] = lib_well.name
        transfer.values["volume_nl"] = echo_settings.values["volume_nl"]

    for idx, xtal_well in enumerate(xtal_wells[:2]):
        arrival = base_time + timedelta(minutes=5 + 10 * idx)
        departure = arrival + timedelta(minutes=5)
        harvest = xtal_well.properties["harvest"]
        harvest.values["arrival_time"] = arrival
        harvest.values["departure_time"] = departure
        harvest.values["puck"] = puck.name
        harvest.values["pin_position"] = idx + 1
        harvest.values["lsdc_name"] = f"{campaign.meta_data['target']}-{idx + 1:02d}"
        harvest.values["harvested"] = True

        pin = pins[idx]
        pin_mount = pin.properties["mount"]
        pin_mount.values["sample_name"] = harvest.values["lsdc_name"]
        pin_mount.values["departure_time"] = departure

        source_name = xtal_well.properties["transfer"].values["source_well"]
        source_well = next(w for w in library_wells if w.name == source_name)
        source_well.properties["well_status"].values["used"] = True

    db_session.flush()

    echo_rows = []
    for xtal_well in xtal_wells:
        transfer = xtal_well.properties["transfer"].values
        mapping = xtal_well.properties["mapping"].values
        drop = xtal_well.properties["drop_location"].values
        echo_rows.append(
            {
                "plate_batch": f"{xtal_plate.name}-{echo_settings.values['batch_id']}",
                "source": transfer["source_well"],
                "destination": mapping["echo_position"],
                "volume": transfer["volume_nl"],
                "x_offset": mapping["well_origin_x"] + drop["x_offset"],
                "y_offset": mapping["well_origin_y"] + drop["y_offset"],
            }
        )

    assert [row["destination"] for row in echo_rows] == ["A1", "A2", "A3"]
    assert echo_rows[1]["y_offset"] == 1650  # 1350 well origin + 300 drop offset
    assert all(row["volume"] == 25.0 for row in echo_rows)

    manifest = []
    for xtal_well in xtal_wells:
        harvest = xtal_well.properties["harvest"].values
        if harvest["harvested"]:
            manifest.append(
                {
                    "xtal_well": xtal_well.name,
                    "destination": harvest["puck"],
                    "pin": harvest["pin_position"],
                    "sample": harvest["lsdc_name"],
                }
            )

    assert [entry["sample"] for entry in manifest] == ["mpro-01", "mpro-02"]
    assert manifest[1]["pin"] == 2
    assert library_wells[0].properties["well_status"].values["used"] is True
    assert library_wells[-1].properties["well_status"].values["used"] is False

    summary = []
    for xtal_well in xtal_wells:
        harvest = xtal_well.properties["harvest"].values
        if not harvest["harvested"]:
            continue
        source = xtal_well.properties["transfer"].values["source_well"]
        library_well = next(w for w in library_wells if w.name == source)
        content = library_well.properties["content"].values
        departure = harvest["departure_time"]
        arrival = harvest["arrival_time"]
        summary.append(
            {
                "xtal_well": xtal_well.name,
                "catalog_id": content["catalog_id"],
                "smiles": content["smiles"],
                "lsdc_sample": harvest["lsdc_name"],
                "soak_min": round((departure - base_time).total_seconds() / 60, 1),
                "harvest_sec": round((departure - arrival).total_seconds(), 1),
            }
        )

    assert summary[0]["soak_min"] == 10.0
    assert summary[0]["harvest_sec"] == 300.0
    assert summary[1]["catalog_id"] == "CAT-002"
