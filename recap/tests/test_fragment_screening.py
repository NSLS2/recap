from sqlalchemy import select

from recap.models.step import StepTemplate
from recap.utils.general import generate_uppercase_alphabets


def test_fragment_screening(db_session):
    from recap.models.attribute import (
        AttributeTemplate,
        AttributeValueTemplate,
    )  # noqa
    from recap.models.resource import (
        Resource,
        ResourceTemplate,
        ResourceType,
    )  # noqa

    """
    Testing fragment screening

    - Create container Resource type
    - Library plate creation
        - Create a library plate template
        - Create library well templates and associate with plate template
        - Create a library plate resource
    - Xtal plate creation
        - Create an xtal plate template
        - Create xtal well type template and associate with xtal template
        - Create Xtal plate resource
    - Process Template creation
        - Create Process template
        - Create resource slots
        - Create Step templates and connect them to resource slots
    - Process Creation
        - Create Process from template
        - Add resources to the process
    """
    # Create container Resource type
    container_type = ResourceType(name="container")

    # Create a library plate template
    lib_plate_1536_template = ResourceTemplate(
        name="Library Plate 1536", types=[container_type]
    )
    plate_dimensions_attr = AttributeTemplate(
        name="LB1536_dimensions",
    )

    num_rows_attr_val = AttributeValueTemplate(
        name="rows",
        value_type="int",
        default_value="32",
    )
    num_cols_attr_val = AttributeValueTemplate(
        name="columns",
        value_type="int",
        default_value="48",
    )
    plate_dimensions_attr.value_templates.append(num_rows_attr_val)
    plate_dimensions_attr.value_templates.append(num_cols_attr_val)

    lib_plate_1536_template.attribute_templates.append(plate_dimensions_attr)
    db_session.add(lib_plate_1536_template)
    db_session.commit()
    statement = select(ResourceTemplate).where(
        ResourceTemplate.name == "Library Plate 1536"
    )
    lib_plate_1536_template = db_session.scalars(statement).one()
    assert (
        lib_plate_1536_template.attribute_templates[0].value_templates[0].default_value
        == "32"
    )

    # Create library well templates and associate with plate template
    a_to_af = generate_uppercase_alphabets(32)
    lib_well_type_names_1536 = [
        {"name": f"{i}{str(j).zfill(2)}"} for i in a_to_af for j in range(1, 49)
    ]

    # Well attributes
    used = AttributeTemplate(
        name="well_status",
    )
    used_value = AttributeValueTemplate(
        name="used", value_type="bool", default_value="true"
    )
    used.value_templates.append(used_value)
    content_attr = AttributeTemplate(
        name="content",
    )
    catalog_id = AttributeValueTemplate(
        name="catalog_id", value_type="str", default_value=""
    )
    smiles = AttributeValueTemplate(name="SMILES", value_type="str", default_value="")
    sequence = AttributeValueTemplate(
        name="sequence", value_type="int", default_value="0"
    )

    content_attr.value_templates.append(catalog_id)
    content_attr.value_templates.append(smiles)
    content_attr.value_templates.append(sequence)

    for well_data in lib_well_type_names_1536:
        well = ResourceTemplate(name=well_data["name"], types=[container_type])
        well.attribute_templates.append(used)
        well.attribute_templates.append(content_attr)
        db_session.add(well)
        lib_plate_1536_template.children.append(well)

    db_session.commit()
    statement = select(ResourceTemplate).where(
        ResourceTemplate.name == "Library Plate 1536"
    )
    lib_plate_1536_template = db_session.scalars(statement).one()
    assert lib_plate_1536_template.children[0].name == "A01"

    # Create a library plate resource
    lib_plate = Resource(
        name="Test LP1536",
        template=lib_plate_1536_template,
    )
    db_session.add(lib_plate)
    db_session.commit()

    statement = select(Resource).where(Resource.name == "Test LP1536")
    lib_plate = db_session.scalars(statement).one()
    assert lib_plate.children[0].template.name == "A01"
    assert lib_plate.properties["LB1536_dimensions"].values["rows"] == 32

    from recap.models.attribute import (
        AttributeTemplate,
        AttributeValueTemplate,
    )  # noqa
    from recap.models.resource import Resource, ResourceTemplate  # noqa

    # - Create an xtal plate template
    xtal_plate_type = ResourceTemplate(name="SwissCI-MRC-2d", types=[container_type])
    a_to_h = generate_uppercase_alphabets(8)
    a_to_p = generate_uppercase_alphabets(16)

    echo = [f"{i}{j}" for i in a_to_p for j in range(1, 13)]
    shifter = [f"{i}{k}{j}" for i in a_to_h for j in ["a", "b"] for k in range(1, 13)]
    plate_maps = [
        {"echo": i, "shifter": j} for i, j in zip(echo, shifter, strict=False)
    ]

    well_position = AttributeTemplate(name="well_position")
    well_pos_x = AttributeValueTemplate(name="x", value_type="int", default_value="0")
    well_pos_y_offset_0 = AttributeValueTemplate(
        name="y_0",
        value_type="int",
        default_value="0",
    )
    well_pos_y_offset_1350 = AttributeValueTemplate(
        name="y_1350",
        value_type="int",
        default_value="1350",
    )
    well_position.value_templates.append(well_pos_x)
    well_position.value_templates.append(well_pos_y_offset_0)
    well_position.value_templates.append(well_pos_y_offset_1350)

    # - Create xtal well type template and associate with xtal template
    for plate_map in plate_maps:
        echo_pos_attr = AttributeTemplate(name="echo_pos")
        x_offset = well_pos_x
        if plate_map["shifter"][-1] == "b":
            y_offset = well_pos_y_offset_0
        else:
            y_offset = well_pos_y_offset_1350

        xtal_well_type = ResourceTemplate(
            name=plate_map["shifter"],
            types=[container_type],
        )
        echo_pos = AttributeValueTemplate(
            name=f"echo_pos_{plate_map['echo']}",
            value_type="str",
            default_value=plate_map["echo"],
        )
        xtal_well_type.attribute_templates.append(echo_pos_attr)
        echo_pos_attr.value_templates.append(x_offset)
        echo_pos_attr.value_templates.append(y_offset)
        echo_pos_attr.value_templates.append(echo_pos)
        xtal_plate_type.children.append(xtal_well_type)

    # - Create Xtal plate resource
    xtal_plate = Resource(name="TestXtalPlate", template=xtal_plate_type)
    db_session.add(xtal_plate)
    db_session.commit()

    statement = select(Resource).where(Resource.name == "TestXtalPlate")
    xtal_plate = db_session.scalars(statement).one()
    assert xtal_plate.children[0].template.name == "A1a"

    # - Create Process template
    from recap.models.process import (
        ProcessRun,
        ProcessTemplate,
        ResourceSlot,
    )  # noqa

    process_template = ProcessTemplate(
        name="Fragment Screening Sample Prep", version="1.0"
    )
    #     - Create resource slots
    lib_plate_resource_slot = ResourceSlot(
        process_template=process_template,
        resource_type=container_type,
        name="library_plate",
        direction="input",
    )
    xtal_plate_resource_slot = ResourceSlot(
        process_template=process_template,
        resource_type=container_type,
        name="xtal_plate",
        direction="input",
    )
    puck_tray_resource_slot = ResourceSlot(
        process_template=process_template,
        resource_type=container_type,
        name="puck_tray",
        direction="output",
    )
    process_template.resource_slots.append(lib_plate_resource_slot)
    process_template.resource_slots.append(xtal_plate_resource_slot)
    process_template.resource_slots.append(puck_tray_resource_slot)
    db_session.add(process_template)
    db_session.commit()

    statement = select(ProcessTemplate).where(
        ProcessTemplate.name == "Fragment Screening Sample Prep"
    )
    process_template: ProcessTemplate = db_session.scalars(statement).one()
    assert any(slot.name == "library_plate" for slot in process_template.resource_slots)

    puck_collection_template = ResourceTemplate(
        name="Puck collection template",
        types=[container_type],
    )
    puck_collection = Resource(
        name="PuckCollection",
        template=puck_collection_template,
    )
    db_session.add(puck_collection)
    db_session.commit()

    #     - Create Step templates and connect them to resource slots
    # Steps for fragment screening
    # 1. Image plate
    drop_volume_val = AttributeValueTemplate(
        name="volume", value_type="float", unit="nL", default_value=0
    )
    drop_volume_attr = AttributeTemplate(
        name="drop_volume", value_templates=[drop_volume_val]
    )
    image_plate_template = StepTemplate(
        name="Image plate",
        process_template=process_template,
        attribute_templates=[drop_volume_attr],
    )
    image_plate_template.resource_slots["xtal_container"] = xtal_plate_resource_slot

    # 2. Echo transfer
    volume_transferred_val = AttributeValueTemplate(
        name="volume", value_type="float", unit="nL", default_value=0
    )
    volume_transferred_attr = AttributeTemplate(
        name="volume_transferred", value_templates=[volume_transferred_val]
    )
    batch_number_value = AttributeValueTemplate(
        name="batch", value_type="int", default_value=0
    )
    batch_number_attr = AttributeTemplate(
        name="batch_number", value_templates=[batch_number_value]
    )
    echo_tx_template = StepTemplate(
        name="Echo Transfer",
        process_template=process_template,
        attribute_templates=[volume_transferred_attr, batch_number_attr],
    )
    echo_tx_template.resource_slots["source_container"] = lib_plate_resource_slot
    echo_tx_template.resource_slots["dest_container"] = xtal_plate_resource_slot

    # 3. Harvesting
    time_departure_val = AttributeValueTemplate(
        name="departure_time", value_type="datetime"
    )
    time_arrival_val = AttributeValueTemplate(
        name="arrival_time", value_type="datetime"
    )
    lsdc_sample_val = AttributeValueTemplate(name="sample_name", value_type="str")
    comment_val = AttributeValueTemplate(name="comment", value_type="str")
    harvesting_status_val = AttributeValueTemplate(name="status", value_type="bool")
    harvesting_attr = AttributeTemplate(
        name="harvesting",
        value_templates=[
            time_departure_val,
            time_arrival_val,
            comment_val,
            harvesting_status_val,
        ],
    )
    lsdc_attr = AttributeTemplate(name="lsdc", value_templates=[lsdc_sample_val])
    harvesting_step_template = StepTemplate(
        name="Harvesting",
        process_template=process_template,
        attribute_templates=[harvesting_attr, lsdc_attr],
    )
    harvesting_step_template.resource_slots["source_container"] = (
        xtal_plate_resource_slot
    )
    harvesting_step_template.resource_slots["dest_container"] = puck_tray_resource_slot

    db_session.add(image_plate_template)
    db_session.add(echo_tx_template)
    db_session.add(harvesting_step_template)
    db_session.commit()

    process_template.step_templates.extend(
        [image_plate_template, echo_tx_template, harvesting_step_template]
    )
    db_session.add(process_template)
    db_session.commit()

    #     - Create Process from template
    process_run = ProcessRun(name="FS1", description="Test", template=process_template)
    #     - Add resources to the process
    for resource in [
        (lib_plate, lib_plate_resource_slot),
        (xtal_plate, xtal_plate_resource_slot),
        (puck_collection, puck_tray_resource_slot),
    ]:
        process_run.resources[resource[1]] = resource[0]

    db_session.add(process_run)
    db_session.commit()

    statement = select(ProcessRun).where(ProcessRun.name == "FS1")
    process_run: ProcessRun = db_session.scalars(statement).one()
    assert process_run.steps[0].template.name == "Image plate"
    assert process_run.steps[0].parameters["drop_volume"].values["volume"] == 0
    assert process_run.steps[1].parameters["volume_transferred"].values["volume"] == 0
    assert process_run.steps[1].parameters["batch_number"].values["batch"] == 0
