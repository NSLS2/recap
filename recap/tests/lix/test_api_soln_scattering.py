from itertools import product

from recap.dsl.process_builder import ProcessRunBuilder
from recap.utils.general import Direction


def test_client(client):
    with client.build_process_template("Test", "0.0.1") as ed:
        ed.add_resource_slot(
            "Input plate 1", "container", Direction.input, create_resource_type=True
        ).add_resource_slot(
            "Input plate 2", "container", Direction.input
        ).add_resource_slot(
            "Liquid transfer operator",
            "operator",
            Direction.input,
            create_resource_type=True,
        ).add_step("Transfer").bind_slot("source", "Input plate 1").bind_slot(
            "dest", "Input plate 2"
        ).bind_slot("operator", "Liquid transfer operator").param_group(
            "volume transfer"
        ).add_attribute(
            attr_name="volume", value_type="float", unit="uL", default=0.0
        ).add_attribute(
            attr_name="rate", value_type="float", unit="uL/sec", default=0.0
        ).close_group().close_step().add_step("Heat plate").bind_slot(
            "target", "Input plate 2"
        ).param_group("heat to").add_attribute(
            "temperature", "float", "degC", "0.0"
        ).close_group().close_step()

    with client.build_resource_template(
        name="96 well plate", type_names=["container", "plate"]
    ) as rt:
        rt.prop_group("dimensions").add_attribute("rows", "float", "", 8).add_attribute(
            "columns", "float", "", 12
        )
        well_cols = "ABCDEFGH"
        well_rows = [i for i in range(1, 13)]
        well_names = [f"{wn[0]}{wn[1]}" for wn in product(well_cols, well_rows)]
        for well_name in well_names:
            rt.add_child(well_name, ["container", "well"]).prop_group(
                group_name="well_data"
            ).add_attribute(
                attr_name="sample_name", value_type="str", unit="", default=""
            ).add_attribute(
                attr_name="buffer_name",
                value_type="str",
                unit="",
                default="",
            ).add_attribute(
                attr_name="volume",
                value_type="int",
                unit="uL",
                default="0",
            ).add_attribute(
                attr_name="mixing",
                value_type="str",
                unit="",
                default="",
            ).add_attribute(
                attr_name="stock",
                value_type="bool",
                unit="",
                default="False",
            ).add_attribute(
                attr_name="notes",
                value_type="str",
                unit="",
                default="",
            ).close_group().close_child()

    with client.build_resource_template(
        name="sample holder", type_names=["container", "plate"]
    ) as rt:
        rt.prop_group("dimensions").add_attribute("rows", "int", "", 2).add_attribute(
            "columns", "int", "", 9
        )
        for well_num in range(1, 19):
            rt.add_child(str(well_num), ["container", "well"]).prop_group(
                group_name="sample_holder_well_data"
            ).add_attribute(
                attr_name="sample_name",
                value_type="str",
                unit="",
                default="",
            ).add_attribute(
                attr_name="buffer_name",
                value_type="str",
                unit="",
                default="",
            ).add_attribute(
                attr_name="volume",
                value_type="float",
                unit="uL",
                default="0",
            ).close_group().close_child()

    with client.build_resource_template(
        name="robot", type_names=["robot", "liquid_transfer", "operator"]
    ) as rt:
        rt.prop_group("details").add_attribute(
            "serial_no", "str", "", "xyz"
        ).close_group()

    client.create_campaign(name="Test campaign", proposal="1")

    sample_holder = client.create_resource("Test destination plate", "sample holder")
    source_plate = client.create_resource("96 well plate", "96 well plate")
    robot = client.create_resource("LHR", "robot")

    with client.build_process_run(
        name="test_run",
        description="This is a test",
        template_name="Test",
        version="0.0.1",
    ) as run:
        run: ProcessRunBuilder
        run.assign_resource(
            resource_slot_name="Input plate 1", resource=source_plate
        ).assign_resource(
            resource_slot_name="Input plate 2", resource=sample_holder
        ).assign_resource("Liquid transfer operator", resource=robot)

        transfer_params = run.get_params("Transfer")
        transfer_params.volume_transfer.volume = 50
        transfer_params.volume_transfer.rate = 1
        run.set_params(transfer_params)

        heat_params = run.get_params("Heat plate")
        heat_params.heat_to.temperature = 100
        run.set_params(heat_params)
