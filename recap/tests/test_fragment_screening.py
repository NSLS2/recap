from recap.utils.general import generate_uppercase_alphabets


def test_library_plate(db_session):
    from recap.models.container import ContainerType, Container
    from recap.models.attribute import Attribute

    lib_plate_1536_template = ContainerType(name="Library Plate 1536", short_name="LP1536")
    num_rows_attr = Attribute(
        name="Number of rows in Library Plate 1536",
        short_name="row_num_LB1536",
        value_type="int",
        default_value="32",
    )
    num_cols_attr = Attribute(
        name="Number of columns for Library Plate 1536",
        short_name="num_cols_LB1536",
        value_type="int",
        default_value="48",
    )
    lib_plate_1536_template.attributes.append(num_rows_attr)
    lib_plate_1536_template.attributes.append(num_cols_attr)

    db_session.add(lib_plate_1536_template)
    db_session.commit()
    result = db_session.query(ContainerType).filter_by(short_name="LP1536").first()
    assert result.attributes[0].default_value == "32"

    a_to_af = generate_uppercase_alphabets(32)
    lib_well_type_names_1536 = [{"name": f"{i}{str(j).zfill(2)}"} for i in a_to_af for j in range(1, 49)]

    # Well attributes
    used = Attribute(name="well_used", short_name="well_used", value_type="bool", default_value="true")
    catalog_id = Attribute(name="Catalog ID", short_name="catalog_id", value_type="str", default_value="")
    smiles = Attribute(name="SMILES code", short_name="SMILES", value_type="str", default_value="")
    sequence = Attribute(name="Sequence", short_name="sequence", value_type="int", default_value="0")

    for well_data in lib_well_type_names_1536:
        well = ContainerType(name=well_data["name"], short_name=well_data["name"])
        well.attributes.append(used)
        well.attributes.append(catalog_id)
        well.attributes.append(smiles)
        well.attributes.append(sequence)
        lib_plate_1536_template.children.append(well)

    db_session.commit()
    result = db_session.query(ContainerType).filter_by(short_name="LP1536").first()
    assert result.children[0].name == "A01"

    lib_plate = Container(name="Test LP1536", short_name="Test_LP1536", container_type=lib_plate_1536_template)
    db_session.add(lib_plate)
    db_session.commit()

    result = db_session.query(Container).filter_by(short_name="Test_LP1536").first()
    assert result.children[0].container_type.name == "A01"
    assert result.properties[0].value == 32


def test_xtal_plate(db_session):
    from recap.models.container import ContainerType, Container
    from recap.models.attribute import Attribute

    xtal_plate_type = ContainerType(name="SwissCI-MRC-2d", short_name="swiss_ci")
    a_to_h = generate_uppercase_alphabets(8)
    a_to_p = generate_uppercase_alphabets(16)

    echo = [f"{i}{j}" for i in a_to_p for j in range(1, 13)]
    shifter = [f"{i}{k}{j}" for i in a_to_h for j in ["a", "b"] for k in range(1, 13)]
    plate_maps = [{"echo": i, "shifter": j} for i, j in zip(echo, shifter)]
    well_pos_x = Attribute(name="well_pos_x", short_name="well_pos_x", value_type="int", default_value="0")
    well_pos_y_offset_0 = Attribute(
        name="well_pos_y_0", short_name="well_pos_y_0", value_type="int", default_value="0"
    )
    well_pos_y_offset_1350 = Attribute(
        name="well_pos_y_1350", short_name="well_pos_y_1350", value_type="int", default_value="1350"
    )

    for plate_map in plate_maps:
        x_offset = well_pos_x
        if plate_map["shifter"][-1] == "b":
            y_offset = well_pos_y_offset_0
        else:
            y_offset = well_pos_y_offset_1350
        # well_map = WellMap(well_pos_x=x_offset, well_pos_y=y_offset, **plate_map)

        xtal_well_type = ContainerType(name=plate_map["shifter"], short_name=plate_map["shifter"])
        echo_pos = Attribute(
            name=f"echo_pos_{plate_map['echo']}",
            short_name=f"echo_pos_{plate_map['echo']}",
            value_type="str",
            default_value=plate_map["echo"],
        )
        xtal_well_type.attributes.append(x_offset)
        xtal_well_type.attributes.append(y_offset)
        xtal_well_type.attributes.append(echo_pos)
        xtal_plate_type.children.append(xtal_well_type)

    xtal_plate = Container(name="TestXtalPlate", short_name="TestXtalPlate", container_type=xtal_plate_type)
    db_session.add(xtal_plate)
    db_session.commit()

    result = db_session.query(Container).filter_by(name="TestXtalPlate").first()
    assert result.children[0].container_type.name == "A1a"
