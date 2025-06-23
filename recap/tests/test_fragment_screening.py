from recap.utils.general import generate_uppercase_alphabets


def test_library_plate(db_session):
    from recap.models.attribute import Attribute
    from recap.models.resource import Resource, ResourceTemplate, ResourceType

    container_type = ResourceType(name="container")

    lib_plate_1536_template = ResourceTemplate(
        name="Library Plate 1536", ref_name="LP1536", type=container_type
    )
    num_rows_attr = Attribute(
        name="Number of rows in Library Plate 1536",
        ref_name="row_num_LB1536",
        value_type="int",
        default_value="32",
    )
    num_cols_attr = Attribute(
        name="Number of columns for Library Plate 1536",
        ref_name="num_cols_LB1536",
        value_type="int",
        default_value="48",
    )
    lib_plate_1536_template.attributes.append(num_rows_attr)
    lib_plate_1536_template.attributes.append(num_cols_attr)

    db_session.add(lib_plate_1536_template)
    db_session.commit()
    result = db_session.query(ResourceTemplate).filter_by(ref_name="LP1536").first()
    assert result.attributes[0].default_value == "32"
    lib_plate_1536_template = result

    a_to_af = generate_uppercase_alphabets(32)
    lib_well_type_names_1536 = [
        {"name": f"{i}{str(j).zfill(2)}"} for i in a_to_af for j in range(1, 49)
    ]

    # Well attributes
    used = Attribute(
        name="well_used", ref_name="well_used", value_type="bool", default_value="true"
    )
    catalog_id = Attribute(
        name="Catalog ID", ref_name="catalog_id", value_type="str", default_value=""
    )
    smiles = Attribute(
        name="SMILES code", ref_name="SMILES", value_type="str", default_value=""
    )
    sequence = Attribute(
        name="Sequence", ref_name="sequence", value_type="int", default_value="0"
    )

    for well_data in lib_well_type_names_1536:
        well = ResourceTemplate(
            name=well_data["name"], ref_name=well_data["name"], type=container_type
        )
        well.attributes.append(used)
        well.attributes.append(catalog_id)
        well.attributes.append(smiles)
        well.attributes.append(sequence)
        lib_plate_1536_template.children.append(well)

    db_session.commit()
    result = db_session.query(ResourceTemplate).filter_by(ref_name="LP1536").first()
    assert result.children[0].name == "A01"

    lib_plate = Resource(
        name="Test LP1536",
        ref_name="Test_LP1536",
        template=lib_plate_1536_template,
    )
    db_session.add(lib_plate)
    db_session.commit()

    result = db_session.query(Resource).filter_by(ref_name="Test_LP1536").first()
    assert result.children[0].template.name == "A01"
    assert result.properties[0].value == 32


def test_xtal_plate(db_session):
    from recap.models.attribute import Attribute
    from recap.models.resource import Resource, ResourceTemplate, ResourceType

    container_type = ResourceType(name="container")
    xtal_plate_type = ResourceTemplate(
        name="SwissCI-MRC-2d", ref_name="swiss_ci", type=container_type
    )
    a_to_h = generate_uppercase_alphabets(8)
    a_to_p = generate_uppercase_alphabets(16)

    echo = [f"{i}{j}" for i in a_to_p for j in range(1, 13)]
    shifter = [f"{i}{k}{j}" for i in a_to_h for j in ["a", "b"] for k in range(1, 13)]
    plate_maps = [{"echo": i, "shifter": j} for i, j in zip(echo, shifter)]
    well_pos_x = Attribute(
        name="well_pos_x", ref_name="well_pos_x", value_type="int", default_value="0"
    )
    well_pos_y_offset_0 = Attribute(
        name="well_pos_y_0",
        ref_name="well_pos_y_0",
        value_type="int",
        default_value="0",
    )
    well_pos_y_offset_1350 = Attribute(
        name="well_pos_y_1350",
        ref_name="well_pos_y_1350",
        value_type="int",
        default_value="1350",
    )

    for plate_map in plate_maps:
        x_offset = well_pos_x
        if plate_map["shifter"][-1] == "b":
            y_offset = well_pos_y_offset_0
        else:
            y_offset = well_pos_y_offset_1350
        # well_map = WellMap(well_pos_x=x_offset, well_pos_y=y_offset, **plate_map)

        xtal_well_type = ResourceTemplate(
            name=plate_map["shifter"],
            ref_name=plate_map["shifter"],
            type=container_type,
        )
        echo_pos = Attribute(
            name=f"echo_pos_{plate_map['echo']}",
            ref_name=f"echo_pos_{plate_map['echo']}",
            value_type="str",
            default_value=plate_map["echo"],
        )
        xtal_well_type.attributes.append(x_offset)
        xtal_well_type.attributes.append(y_offset)
        xtal_well_type.attributes.append(echo_pos)
        xtal_plate_type.children.append(xtal_well_type)

    xtal_plate = Resource(
        name="TestXtalPlate", ref_name="TestXtalPlate", template=xtal_plate_type
    )
    db_session.add(xtal_plate)
    db_session.commit()

    result = db_session.query(Resource).filter_by(name="TestXtalPlate").first()
    assert result.children[0].template.name == "A1a"
