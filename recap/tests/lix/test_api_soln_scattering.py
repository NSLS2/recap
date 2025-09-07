from itertools import product

from recap.client.base_client import RecapClient
from recap.models.process import Direction


def test_client(db_session):
    client = RecapClient(session=db_session)

    with client.process_template("Test", "0.0.1") as ed:
        ed.add_resource_slot(
            "Input plate 1", "container", Direction.input, create_resource_type=True
        ).add_resource_slot(
            "Input plate 2", "container", Direction.input
        ).add_resource_slot(
            "Liquid transfer operator",
            "operator",
            Direction.input,
            create_resource_type=True,
        ).add_step("Transfer").bind("Input plate 1", "source").bind(
            "Input plate 2", "dest"
        ).bind("Liquid transfer operator", "operator").add_param(
            "volume transfer", "volume", "float", "uL", "0.0", create_group=True
        ).add_param(
            "volume transfer", "rate", "float", "uL/sec", "0.0"
        ).complete_step().add_step("Heat plate").bind(
            "Input plate 2", "target"
        ).add_param(
            "heat to", "temperature", "float", "degC", "0.0", create_group=True
        ).complete_step()

    with client.resource_template("96 well plate", ["container", "plate"]) as rt:
        rt.add_prop("dimensions", "rows", "float", "", 8, create_group=True).add_prop(
            "dimensions", "columns", "float", "", 12
        )
        well_cols = "ABCDEFGH"
        well_rows = [i for i in range(1, 13)]
        well_names = [f"{wn[0]}{wn[1]}" for wn in product(well_cols, well_rows)]
        for well_name in well_names:
            rt.add_child(well_name, ["container", "well"]).add_prop(
                group_name="well_data",
                prop_name="sample_name",
                value_type="str",
                unit="",
                default="",
                create_group=True,
            ).add_prop(
                group_name="well_data",
                prop_name="buffer_name",
                value_type="str",
                unit="",
                default="",
            ).add_prop(
                group_name="well_data",
                prop_name="volume",
                value_type="int",
                unit="uL",
                default="0",
            ).add_prop(
                group_name="well_data",
                prop_name="mixing",
                value_type="str",
                unit="",
                default="",
            ).add_prop(
                group_name="well_data",
                prop_name="stock",
                value_type="bool",
                unit="",
                default="False",
            ).add_prop(
                group_name="well_data",
                prop_name="notes",
                value_type="str",
                unit="",
                default="",
            ).complete_child()

    with client.resource_template("sample holder", ["container", "plate"]) as rt:
        rt.add_prop("dimensions", "rows", "int", "", 2, create_group=True).add_prop(
            "dimensions", "columns", "int", "", 9
        )
        for well_num in range(1, 19):
            rt.add_child(str(well_num), ["container", "well"]).add_prop(
                group_name="sample_holder_well_data",
                prop_name="sample_name",
                value_type="str",
                unit="",
                default="",
                create_group=True,
            ).add_prop(
                group_name="sample_holder_well_data",
                prop_name="buffer_name",
                value_type="str",
                unit="",
                default="",
            ).add_prop(
                group_name="sample_holder_well_data",
                prop_name="volume",
                value_type="float",
                unit="uL",
                default="0",
            ).complete_child()

    with client.process_run(
        name="test_run", template_name="Test", version="0.0.1"
    ) as run:
        run.create_resource("96 well plate", "96 well plate")
        run.create_resource("Test destination plate", "sample holder")
        run.assign_resource(
            "Input plate 1", resource_name="96 well plate"
        ).assign_resource(
            "Input plate 2", resource_name="Test destination plate"
        )  # .assign_resource("Liquid transfer operator", resource_name="Robot XYZ")

        transfer_params = run.get_params("Transfer")
        transfer_params.volume_transfer.volume = 50
        transfer_params.volume_transfer.rate = 1
        print(transfer_params)
        run.set_params(transfer_params)

        heat_params = run.get_params("Heat plate")
        heat_params.heat_to.temperature = 100
        run.set_params(heat_params)

        # run.close()
