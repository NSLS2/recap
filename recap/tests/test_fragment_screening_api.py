from recap.client.base_client import RecapClient
from recap.models.process import Direction
from recap.utils.general import generate_uppercase_alphabets


def test_fragment_screening_api(db_session):
    client = RecapClient(session=db_session)

    with client.resource_template(
        "Library Plate 1536", type_names=["container", "plate"]
    ) as lp:
        lp.prop_group("LB1536_dimensions").add_attribute(
            "rows", "int", "", 32
        ).add_attribute("columns", "int", "", 48).close_group()

        a_to_af = generate_uppercase_alphabets(32)
        lib_well_type_names_1536 = [
            {"name": f"{i}{str(j).zfill(2)}"} for i in a_to_af for j in range(1, 49)
        ]
        for well_data in lib_well_type_names_1536:
            lp.add_child(well_data["name"], ["container", "well"]).prop_group(
                "well_status"
            ).add_attribute("used", "bool", "", False).close_group().prop_group(
                "content"
            ).add_attribute("catalog_id", "str", "", "").add_attribute(
                "SMILES", "str", "", ""
            ).add_attribute("sequence", "int", "", 0).close_group().close_child()

    with client.resource_template("SwissCI-MRC-2d", ["container", "plate"]) as plate:
        a_to_h = generate_uppercase_alphabets(8)
        a_to_p = generate_uppercase_alphabets(16)
        echo = [f"{i}{j}" for i in a_to_p for j in range(1, 13)]
        shifter = [
            f"{i}{k}{j}" for i in a_to_h for j in ["a", "b"] for k in range(1, 13)
        ]
        plate_maps = [
            {"echo": i, "shifter": j} for i, j in zip(echo, shifter, strict=False)
        ]

        for plate_map in plate_maps:
            plate_shift_b = plate_map["shifter"][-1] == "b"
            plate.add_child(plate_map["shifter"], ["container", "well"]).prop_group(
                "echo_offset"
            ).add_attribute("x", "int", "", 0).add_attribute(
                "y_0" if plate_shift_b else "y_1350",
                "int",
                "",
                0 if plate_shift_b else 1350,
            ).add_attribute(
                f"echo_pos_{plate_map['echo']}", "str", "", plate_map["echo"]
            ).close_group().close_child()

    with client.resource_template("puck_collection", ["container"]) as puck_collection:
        puck_collection.prop_group("contents").add_attribute("count", "int", "", 0)

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

    xtal_plate = client.create_resource(
        "TestXtalPlate", template_name="Library Plate 1536"
    )
    xtal_plate.save()
    # xtal_props = xtal_plate.get_props()

    client.process_run("Test run", "Fragment Screening Sample Prep", "1.0")
