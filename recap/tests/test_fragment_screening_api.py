from recap.client.base_client import RecapClient
from recap.db.process import Direction
from recap.utils.general import generate_uppercase_alphabets


def test_fragment_screening_api(db_session):
    client = RecapClient(session=db_session)

    # Testing
    # - Create templates
    #     - Library plate
    #     - Xtal plate
    #     - Puck collection
    #     - Puck template
    #     - Pin template
    #     - Process template
    # - Create instance
    #     - Campaign
    #     - ProcessRun
    #     - Library plate
    #     - xtal plate
    #     -

    with client.resource_template(
        "Library Plate 1536", type_names=["container", "library_plate", "plate"]
    ) as lp:
        # lp.prop_group("LB1536_dimensions").add_attribute(
        #     "rows", "int", "", 32
        # ).add_attribute("columns", "int", "", 48).close_group()
        lp.add_properties(
            {
                "LB1536_dimensions": [
                    {"name": "rows", "type": "int", "default": 32},
                    {"name": "columns", "type": "int", "default": 48},
                ]
            }
        )

        a_to_af = generate_uppercase_alphabets(32)
        lib_well_type_names_1536 = [
            {"name": f"{i}{str(j).zfill(2)}"} for i in a_to_af for j in range(1, 49)
        ]
        for well_data in lib_well_type_names_1536:
            # lp.add_child(well_data["name"], ["container", "well"]).prop_group(
            #     "well_status"
            # ).add_attribute("used", "bool", "", False).close_group().prop_group(
            #     "content"
            # ).add_attribute("catalog_id", "str", "", "").add_attribute(
            #     "SMILES", "str", "", ""
            # ).add_attribute("sequence", "int", "", 0).close_group().close_child()
            lp.add_child(well_data["name"], ["container", "well"]).add_properties(
                {
                    "well_status": [
                        {"name": "used", "type": "bool", "default": False},
                    ],
                    "content": [
                        {"name": "catalog_id", "type": "str", "default": ""},
                        {"name": "SMILES", "type": "str", "default": ""},
                        {"name": "sequence", "type": "int", "default": 0},
                    ],
                }
            ).close_child()

    rt = (
        client.query_maker()
        .resource_templates()
        .filter_by_types(["library_plate"])
        .first()
    )
    assert rt.name == "Library Plate 1536"

    with client.resource_template(
        "SwissCI-MRC-2d", ["container", "xtal_plate", "plate"]
    ) as plate:
        a_to_h = generate_uppercase_alphabets(8)
        a_to_p = generate_uppercase_alphabets(16)
        echo = [f"{i}{j}" for i in a_to_p for j in range(1, 13)]
        shifter = [
            f"{i}{k}{j}" for i in a_to_h for j in ["a", "b"] for k in range(1, 13)
        ]
        plate_maps = [
            {"echo": i, "shifter": j} for i, j in zip(echo, shifter, strict=False)
        ]
        # plate.prop_group("metadata").add_attribute("drop_volume", "int", "nL", 0).close_group()
        plate.add_properties(
            {
                "metadata": [
                    {"name": "drop_volume", "type": "float", "unit": "nL", "default": 0}
                ]
            }
        )
        for plate_map in plate_maps:
            plate_shift_b = plate_map["shifter"][-1] == "b"
            plate.add_child(plate_map["shifter"], ["container", "well"]).add_properties(
                {
                    "echo_offset": [
                        {"name": "x", "type": "int", "default": 0},
                        {
                            "name": "y_0" if plate_shift_b else "y_1350",
                            "type": "int",
                            "default": 0 if plate_shift_b else 1350,
                        },
                        {
                            "name": f"echo_pos_{plate_map['echo']}",
                            "type": "str",
                            "default": plate_map["echo"],
                        },
                    ]
                }
            ).close_child()
            # .prop_group(
            #     "echo_offset"
            # ).add_attribute("x", "int", "", 0).add_attribute(
            #     "y_0" if plate_shift_b else "y_1350",
            #     "int",
            #     "",
            #     0 if plate_shift_b else 1350,
            # ).add_attribute(
            #     f"echo_pos_{plate_map['echo']}", "str", "", plate_map["echo"]
            # ).close_group()\

    rt = (
        client.query_maker()
        .resource_templates()
        .filter_by_types(["xtal_plate"])
        .first()
    )
    assert rt.name == "SwissCI-MRC-2d"

    with client.resource_template("puck_collection", ["container"]) as puck_collection:
        puck_collection.prop_group("contents").add_attribute("count", "int", "", 0)

    with client.resource_template("puck", ["container", "puck"]) as puck_template:
        puck_template.prop_group("pin_count").add_attribute(
            "total", "int", "", 16
        ).add_attribute("occupied", "int", "", 0).close_group()

    with client.resource_template("pin", ["container", "pin"]) as pin_template:
        pin_template.prop_group("content").add_attribute(
            "position", "int", "", 0
        ).close_group()

    with client.process_template("Fragment Screening Sample Prep", "1.0") as pt:
        pt.add_resource_slot(
            "library_plate", "plate", Direction.input
        ).add_resource_slot("xtal_plate", "plate", Direction.input).add_resource_slot(
            "puck_tray", "container", Direction.output
        )
        pt.add_step(name="Image plate").param_group("drop").add_attribute(
            "volume", "float", "nL", 0
        ).close_group().bind_slot("xtal_container", "xtal_plate").close_step()
        pt.add_step(name="Echo transfer").param_group("volume").add_attribute(
            "transferred", "float", "uL", 0
        ).close_group().param_group("batch").add_attribute(
            "number", "int", "", 0
        ).close_group().bind_slot("source_container", "library_plate").bind_slot(
            "dest_container", "xtal_plate"
        ).close_step()

        pt.add_step(name="Harvesting").param_group("harvesting").add_attribute(
            "departure_time", "datetime", "", None
        ).add_attribute("arrival_time", "datetime", "", None).add_attribute(
            "comment", "str", "", ""
        ).add_attribute("status", "str", "", "").close_group().param_group(
            "lsdc"
        ).add_attribute("sample_name", "str", "", "").close_group().bind_slot(
            "source_container", "xtal_plate"
        ).bind_slot("dest_container", "puck_tray").close_step()

    client.create_campaign("Test campaign", "123", "0")

    xtal_plate_template = (
        client.query_maker()
        .resource_templates()
        .filter_by_types(["xtal_plate"])
        .first()
    )
    if xtal_plate_template:
        client.create_resource("Test Xtal plate", xtal_plate_template.name)

    library_plate_template = (
        client.query_maker()
        .resource_templates()
        .filter_by_types(["library_plate"])
        .filter(name="Library Plate 1536")
        .first()
    )
    if library_plate_template:
        with client.create_resource(
            "DSI-poised", library_plate_template.name
        ) as lib_plate_builder:
            for well in lib_plate_builder.resource.children:
                well.properties["content"].values["SMILES"] = ""
                well.properties["content"].values["catalog_id"] = ""

    client.process_run("Test run", "Fragment Screening Sample Prep", "1.0")
