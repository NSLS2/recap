from itertools import product

from sqlalchemy import select

from recap.models.attribute import AttributeTemplate, AttributeValueTemplate
from recap.models.process import Direction, ProcessRun, ProcessTemplate, ResourceSlot
from recap.models.resource import Resource, ResourceTemplate, ResourceType
from recap.models.step import StepTemplate


def test_solution_scattering_sample_prep(db_session):
    """
    Steps for sample prep

    96 well sample plate, each column consists of 12 wells (A1-A12)
    Cols A1-A9 contain buffers or samples
    Cols A10-A12 contain stock solutions always. A1-A9 could be
    used to hold stock solutions

    Each well would have a bool to indicate stock solution

    Mixing column indicates how sample is mixed with stock solution
    if no mixing instruction, there must be a volume value
    Both cannot be empty


    """

    container_resource_type = ResourceType(name="container")

    sample_plate_96_well = ResourceTemplate(
        name="96-well sample template",
        types=[container_resource_type],
    )
    num_rows_attr_val = AttributeValueTemplate(
        name="rows", value_type="int", default_value=8
    )
    num_cols_attr_val = AttributeValueTemplate(
        name="cols", value_type="int", default_value=12
    )
    plate_dimensions_attr = AttributeTemplate(
        name="96_well_plate_dimesions",
        value_templates=[num_rows_attr_val, num_cols_attr_val],
    )
    sample_plate_96_well.attribute_templates.append(plate_dimensions_attr)
    db_session.add(sample_plate_96_well)
    db_session.commit()

    statement = select(ResourceTemplate).where(
        ResourceTemplate.name == "96-well sample template"
    )
    sample_plate_96_well: ResourceTemplate = db_session.scalars(statement).one()
    assert (
        sample_plate_96_well.attribute_templates[0].value_templates[0].default_value
        == "8"
    )

    # Well attributes
    sample_name = AttributeValueTemplate(
        name="sample_name", value_type="str", default_value=""
    )
    buffer_name = AttributeValueTemplate(
        name="buffer_name", value_type="str", default_value=""
    )
    volume = AttributeValueTemplate(name="volume", value_type="int", default_value=0)
    mixing_instruction = AttributeValueTemplate(
        name="mixing", value_type="str", default_value=""
    )
    stock = AttributeValueTemplate(
        name="stock", value_type="bool", default_value="False"
    )
    notes = AttributeValueTemplate(name="notes", value_type="str", default_value="")
    well_data = AttributeTemplate(
        name="well_data",
        value_templates=[
            sample_name,
            buffer_name,
            volume,
            mixing_instruction,
            stock,
            notes,
        ],
    )
    db_session.add(well_data)
    db_session.commit()

    well_cols = "ABCDEFGH"
    well_rows = [i for i in range(1, 13)]
    well_names = [f"{wn[0]}{wn[1]}" for wn in product(well_cols, well_rows)]
    well_resource_type = ResourceType(name="well")
    for well_name in well_names:
        well_resource_template = ResourceTemplate(
            name=well_name, types=[well_resource_type]
        )
        well_resource_template.attribute_templates.append(well_data)
        db_session.add(well_resource_template)
        sample_plate_96_well.children.append(well_resource_template)
    db_session.add(sample_plate_96_well)
    db_session.commit()

    sample_holder = ResourceTemplate(
        name="sample holder template", types=[container_resource_type]
    )
    # Well attributes
    sample_well_data = AttributeTemplate(
        name="sample_holder_well_data",
        value_templates=[sample_name, buffer_name, volume],
    )
    for well_num in range(1, 19):
        well_resource_template = ResourceTemplate(
            name=str(well_num), types=[well_resource_type]
        )
        well_resource_template.attribute_templates.append(sample_well_data)
        db_session.add(well_resource_template)
        sample_holder.children.append(well_resource_template)
    db_session.add(sample_holder)
    db_session.commit()

    # Create process
    sample_prep_process = ProcessTemplate(
        name="Solution Scattering Sample Prep", version="1.0"
    )
    sample_plate_resource_slot = ResourceSlot(
        name="sample_plate",
        process_template=sample_prep_process,
        resource_type=container_resource_type,
        direction=Direction.input,
    )
    holder_resource_slot = ResourceSlot(
        name="holder",
        process_template=sample_prep_process,
        resource_type=container_resource_type,
        direction=Direction.output,
    )
    db_session.add(sample_plate_resource_slot)
    db_session.add(holder_resource_slot)
    db_session.commit()

    volume_attr_value = AttributeValueTemplate(
        name="volume",
        value_type="float",
        unit="uL",
        default_value="0",
    )
    volume_transferred_template = AttributeTemplate(
        name="volume_transferred",
        value_templates=[volume_attr_value],
    )

    # Transfer action
    robot_transfer_action_template = StepTemplate(
        name="Robot transfer",
        process_template=sample_prep_process,
        attribute_templates=[volume_transferred_template],
        # resource_slots=[(sample_plate_resource_slot, "source_container"),
        #                 (holder_resource_slot, "destination_container")]
    )
    robot_transfer_action_template.resource_slots["source_container"] = (
        sample_plate_resource_slot
    )
    robot_transfer_action_template.resource_slots["destination_container"] = (
        holder_resource_slot
    )
    db_session.add(robot_transfer_action_template)
    db_session.commit()

    assert (
        robot_transfer_action_template.resource_slots["source_container"].name
        == "sample_plate"
    )

    # Create process instance
    source_plate = Resource(name="source_plate1", template=sample_plate_96_well)
    destination_plate = Resource(name="CH", template=sample_holder)
    db_session.add(destination_plate)
    db_session.add(source_plate)
    db_session.commit()
    process_run1 = ProcessRun(
        name="Test1", description="This is a test", template=sample_prep_process
    )

    # process_run1.resources=[(source_plate, sample_plate_resource_slot),
    #                         (destination_plate, holder_resource_slot)]
    process_run1.resources[sample_plate_resource_slot] = source_plate
    process_run1.resources[holder_resource_slot] = destination_plate
    db_session.add(process_run1)
    db_session.commit()

    assert process_run1.steps[0].template.name == "Robot transfer"
